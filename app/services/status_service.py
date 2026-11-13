"""设备状态上报服务（手机 APP 代报，README §5.4 / APP 对接说明 §6.3）。

录音卡固件不与后端通信，蓝牙实测值（电量 / 充电 / 录音 / 连接状态）只能由
手机 APP 代报。本模块负责：

- **归属校验**：当前领用员工 + 有效 APP 绑定 + 同租户 + 生命周期允许；
  这是「员工代报」而非设备身份认证，真正的设备证明需固件侧挑战-响应。
- **服务端裁决**：``online`` 由 ``connected`` 与心跳超时共同决定，
  ``updated_at`` 一律写服务端 naive UTC，客户端不可传这两个字段。
- **防乱序 / 防伪造时间**：拒绝未来时间与早于上次接收时间的旧快照，
  时间戳按采集时刻截断，最近通信 / 最近同步时间单调不回退。
- **单行 UPSERT**：以 ``device_id`` 为主键覆盖 ``device_status``，
  高频心跳只更新这一行；仅在在线 / 充电 / 录音跃迁时写一条 device_event。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import BINDABLE_LIFECYCLE_STATUSES, EventType, LifecycleStatus
from app.core.errors import (
    ConflictError,
    DeviceStateError,
    PermissionDeniedError,
    ValidationError,
)
from app.db.models import DeviceStatus
from app.schemas.device import (
    DeviceStatusAck,
    DeviceStatusReport,
    build_status_out,
    to_naive_utc,
)
from app.services.device_service import (
    get_device_row,
    load_active_binding,
    load_current_assignment,
)
from app.services.event_service import record_event
from app.services.transaction import transaction

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clamp(value: Optional[datetime], upper: datetime) -> Optional[datetime]:
    """客户端时间归一为 naive UTC，且不得晚于采集时刻。"""
    naive = to_naive_utc(value)
    if naive is None:
        return None
    return min(naive, upper)


def _newer(old: Optional[datetime], new: Optional[datetime]) -> Optional[datetime]:
    """最近通信 / 最近同步时间单调不回退。"""
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


def report_status(
    db: Session,
    device_id: int,
    payload: DeviceStatusReport,
    *,
    user_refs: Sequence[str],
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    reporter: Optional[str] = None,
) -> DeviceStatusAck:
    """接收一条 APP 状态快照并返回服务端裁决后的状态。"""
    now = _utcnow()
    sampled_at = to_naive_utc(payload.sampled_at)
    skew = timedelta(seconds=settings.device_status_max_clock_skew_seconds)
    ttl = timedelta(seconds=settings.device_online_timeout_seconds)

    # 1) 时间合法性：手机时钟可被修改，未来时间与离线回放的旧快照都不采信。
    if sampled_at > now + skew:
        raise ValidationError(
            "采集时间晚于服务端时间过多，请校准手机时钟后重新上报",
            error_code="invalid_sample_time",
            details={
                "sampled_at": sampled_at.isoformat(),
                "server_time": now.isoformat(),
                "max_clock_skew_seconds": settings.device_status_max_clock_skew_seconds,
            },
        )

    # 2) 归属校验：跨租户按 404，非领用人 / 未绑定 / 不可用设备明确拒绝。
    device = get_device_row(db, device_id, tenant_scope)
    if LifecycleStatus(device.lifecycle_status) not in BINDABLE_LIFECYCLE_STATUSES:
        raise DeviceStateError(
            f"设备当前状态 {device.lifecycle_status} 不允许上报状态"
        )

    refs = {str(v) for v in user_refs}
    assignment = load_current_assignment(db, device.id)
    if assignment is None or assignment.user_id not in refs:
        raise PermissionDeniedError("只有当前领用员工可以上报该设备状态")
    binding = load_active_binding(db, device.id)
    if binding is None or binding.user_id not in refs:
        raise PermissionDeniedError("设备尚未完成 APP 绑定，不能上报状态")

    status = db.get(DeviceStatus, device.id)
    if status is None:
        # 正常入库流程已创建占位行；这里兜底兼容历史数据或手工导入的设备。
        status = DeviceStatus(device_id=device.id)
        db.add(status)

    previous_updated_at = to_naive_utc(status.updated_at)
    if previous_updated_at is not None and sampled_at < previous_updated_at - skew:
        raise ConflictError(
            "状态快照早于服务端已接收的最新快照，已丢弃；请重新采集后再上报",
            error_code="stale_status_report",
            details={
                "sampled_at": sampled_at.isoformat(),
                "previous_updated_at": previous_updated_at.isoformat(),
            },
        )

    # 3) 字段合并：未传的可选字段保留旧值（心跳可以只带变化项）。
    provided = payload.model_dump(exclude_unset=True)
    old_seen = to_naive_utc(status.last_seen_at)
    old_sync = to_naive_utc(status.last_sync_at)
    old = {
        "online": bool(status.online),
        "charging": bool(status.charging),
        "recording": bool(status.recording),
    }

    if payload.connected:
        last_seen = _clamp(payload.last_seen_at, sampled_at) or sampled_at
    else:
        # 断连事件不刷新最近通信时间，避免下次心跳把它误判成刚见过设备。
        last_seen = _clamp(payload.last_seen_at, sampled_at) or old_seen
    last_seen = _newer(old_seen, last_seen)
    last_sync = _newer(old_sync, _clamp(payload.last_sync_at, sampled_at))

    online = (
        bool(payload.connected)
        and last_seen is not None
        and (now - last_seen) <= ttl
    )
    battery = (
        payload.battery_level
        if "battery_level" in provided
        else status.battery_level
    )
    charging = (
        payload.charging if payload.charging is not None else bool(status.charging)
    )
    recording = (
        payload.recording if payload.recording is not None else bool(status.recording)
    )

    changes: Dict[str, Dict[str, Any]] = {}
    for field, value in (
        ("online", online),
        ("charging", charging),
        ("recording", recording),
    ):
        if old[field] != value:
            changes[field] = {"from": old[field], "to": value}

    with transaction(db):
        status.online = online
        status.battery_level = battery
        status.charging = charging
        status.recording = recording
        status.last_seen_at = last_seen
        status.last_sync_at = last_sync
        status.updated_at = now
        db.flush()

        if changes:
            record_event(
                db,
                device.id,
                EventType.DEVICE_STATUS_CHANGED,
                operator_id=operator_id,
                event_data={
                    "changes": changes,
                    "sampled_at": sampled_at.isoformat(),
                    "reporter": reporter,
                    "source": "app",
                },
            )

    logger.info(
        "设备状态上报：device=%s reporter=%s online=%s battery=%s recording=%s",
        device.id,
        reporter or operator_id or "-",
        online,
        battery,
        recording,
    )

    snapshot = build_status_out(status, now=now)
    return DeviceStatusAck(
        **snapshot.model_dump(),
        server_time=now,
        heartbeat_interval_seconds=settings.device_status_heartbeat_seconds,
    )
