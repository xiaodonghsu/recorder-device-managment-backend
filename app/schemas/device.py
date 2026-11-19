"""设备档案相关的请求/响应契约。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import LifecycleStatus
from app.core.security import is_auth_key_plausible


# ---------------------------------------------------------------------------
# device_status.metrics：稀疏遥测扩展槽的约束
#
# 这是一个开放写入口，必须限死结构，否则会变成任意大对象 / 嵌套文档的注入点：
# 只允许一层扁平键值，值只能是标量或一维标量数组；正式字段名禁止占用。
# ---------------------------------------------------------------------------
METRIC_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
METRIC_MAX_KEYS = 20
METRIC_MAX_BYTES = 2048
METRIC_MAX_STRING = 128
METRIC_MAX_LIST_ITEMS = 20
RESERVED_METRIC_KEYS = frozenset(
    {
        "device_id",
        "online",
        "battery_level",
        "charging",
        "recording",
        "sampled_at",
        "connected",
        "last_seen_at",
        "last_sync_at",
        "updated_at",
        "storage_free_bytes",
        "storage_total_bytes",
        "firmware_version",
        "gain",
        "metrics",
    }
)


def _check_metric_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"metrics.{key} 不能是 NaN / Infinity")
        return value
    if isinstance(value, str):
        if len(value) > METRIC_MAX_STRING:
            raise ValueError(f"metrics.{key} 字符串不得超过 {METRIC_MAX_STRING} 字符")
        return value
    if isinstance(value, list):
        if len(value) > METRIC_MAX_LIST_ITEMS:
            raise ValueError(f"metrics.{key} 数组不得超过 {METRIC_MAX_LIST_ITEMS} 项")
        return [_check_metric_value(key, item) for item in value]
    raise ValueError(f"metrics.{key} 只允许标量或一维数组，不支持对象 / 嵌套结构")


def normalize_status_metrics(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """校验并归一化遥测扩展槽，返回可直接写入 JSON 列的 dict。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("metrics 必须是 JSON 对象")
    if len(value) > METRIC_MAX_KEYS:
        raise ValueError(f"metrics 最多 {METRIC_MAX_KEYS} 个键")

    cleaned: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not METRIC_KEY_RE.fullmatch(key):
            raise ValueError("metrics 键名非法：仅允许字母 / 数字 / 下划线，首字符非数字且 ≤32 字符")
        if key in RESERVED_METRIC_KEYS:
            raise ValueError(f"metrics 不能使用正式状态字段名：{key}")
        cleaned[key] = _check_metric_value(key, item)

    payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload.encode("utf-8")) > METRIC_MAX_BYTES:
        raise ValueError(f"metrics 序列化后不得超过 {METRIC_MAX_BYTES} 字节")
    return cleaned


class DeviceCreate(BaseModel):
    """设备入库请求（README §5.1）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    sn: str = Field(min_length=1, max_length=64, description="设备 SN，全局唯一，不允许员工修改")
    bluetooth_name: Optional[str] = Field(default=None, max_length=128, description="蓝牙广播名称，可重复")
    model: Optional[str] = Field(default=None, max_length=64, description="设备型号")
    manufacturer: Optional[str] = Field(default=None, max_length=100, description="厂商")
    hardware_version: Optional[str] = Field(default=None, max_length=32)
    firmware_version: Optional[str] = Field(default=None, max_length=32)
    production_batch: Optional[str] = Field(default=None, max_length=64)
    auth_key: Optional[str] = Field(
        default=None,
        max_length=128,
        description="导入已有 AuthKey；不传则由系统生成高强度随机值",
    )
    tenant_id: Optional[str] = Field(
        default=None, max_length=64, description="目标企业；管理员不填则归属自身企业"
    )

    @field_validator("sn")
    @classmethod
    def _check_sn(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("SN 不能为空")
        if any(ch.isspace() for ch in value):
            raise ValueError("SN 不能包含空白字符")
        return value

    @field_validator("auth_key")
    @classmethod
    def _check_auth_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        if not value:
            return None
        if not is_auth_key_plausible(value):
            raise ValueError("AuthKey 格式非法（长度需为 16-128，且仅含字母数字与 _-）")
        return value


class DeviceBatchCreate(BaseModel):
    """设备批量入库请求（README §8 P0）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    items: List[DeviceCreate] = Field(min_length=1, max_length=1000, description="待入库设备清单")
    continue_on_error: bool = Field(default=True, description="单条失败是否继续处理其余设备")

    @field_validator("items")
    @classmethod
    def _check_unique_sn(cls, v: List[DeviceCreate]) -> List[DeviceCreate]:
        sns = [item.sn for item in v]
        duplicates = {sn for sn in sns if sns.count(sn) > 1}
        if duplicates:
            raise ValueError(f"批量清单中存在重复 SN: {', '.join(sorted(duplicates))}")
        return v


class DeviceBatchResultItem(BaseModel):
    """批量入库的单条结果。"""

    sn: str
    success: bool
    device_id: Optional[int] = None
    auth_key: Optional[str] = Field(default=None, description="系统生成的 AuthKey 明文")
    error: Optional[str] = None


class DeviceBatchResult(BaseModel):
    """批量入库汇总。"""

    total: int
    succeeded: int
    failed: int
    items: List[DeviceBatchResultItem]


class DeviceUpdate(BaseModel):
    """设备档案更新（SN 不可修改）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    bluetooth_name: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=64)
    manufacturer: Optional[str] = Field(default=None, max_length=100)
    hardware_version: Optional[str] = Field(default=None, max_length=32)
    firmware_version: Optional[str] = Field(default=None, max_length=32)
    production_batch: Optional[str] = Field(default=None, max_length=64)


class DeviceStatusOut(BaseModel):
    """设备最新状态（device_status 表）。"""

    model_config = ConfigDict(from_attributes=True)

    device_id: int
    online: bool = False
    battery_level: Optional[int] = None
    charging: bool = False
    recording: bool = False
    storage_free_bytes: Optional[int] = Field(default=None, description="APP 实测剩余容量（字节）")
    storage_total_bytes: Optional[int] = Field(default=None, description="APP 实测总容量（字节）")
    firmware_version: Optional[str] = Field(
        default=None, description="APP 蓝牙实测固件版本（不修改 device 主表档案）"
    )
    gain: Optional[int] = Field(default=None, description="录音增益档位 1-3")
    metrics: Optional[Dict[str, Any]] = Field(default=None, description="稀疏遥测扩展槽（JSON 对象）")
    last_seen_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把客户端 / ORM 时间统一成 naive UTC（与 MySQL DATETIME 语义一致）。

    APP 上报的时间可能带 ``Z`` 或时区偏移；数据库与既有接口一律使用不带
    时区后缀的 UTC 字符串，因此在服务层入口与输出组装处各做一次归一化。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def build_status_out(status: Any, *, now: Optional[datetime] = None) -> DeviceStatusOut:
    """组装状态输出，``online`` 由服务端按心跳超时裁决。

    APP 被杀进程 / 开飞行模式时不会发「离线」事件，仅靠上报写入的
    ``device_status.online`` 会让管理后台永远显示在线。所以所有读取路径
    （设备详情、我的设备、状态端点、表格列表）统一走本函数：
    只有在库里标记为在线、且 ``last_seen_at`` 未超过
    ``DEVICE_ONLINE_TIMEOUT_SECONDS`` 时才视为在线。
    """
    from app.core.config import settings

    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last_seen = to_naive_utc(status.last_seen_at)
    online = (
        bool(status.online)
        and last_seen is not None
        and (current - last_seen).total_seconds() <= settings.device_online_timeout_seconds
    )
    # 新列在存量库执行迁移前可能还不存在，用 getattr 兜底，避免读接口 500。
    return DeviceStatusOut(
        device_id=status.device_id,
        online=online,
        battery_level=status.battery_level,
        charging=bool(status.charging),
        recording=bool(status.recording),
        storage_free_bytes=getattr(status, "storage_free_bytes", None),
        storage_total_bytes=getattr(status, "storage_total_bytes", None),
        firmware_version=getattr(status, "firmware_version", None),
        gain=getattr(status, "gain", None),
        metrics=getattr(status, "metrics", None),
        last_seen_at=last_seen,
        last_sync_at=to_naive_utc(status.last_sync_at),
        updated_at=to_naive_utc(status.updated_at),
    )


class DeviceStatusReport(BaseModel):
    """手机 APP 上报的设备状态快照（README §5.4 / APP 对接说明 §6.3）。

    注意这是「员工代报」，不是设备身份认证：服务端校验当前领用与有效绑定。
    ``extra="forbid"`` 明确拒绝客户端传 ``online`` / ``updated_at``，
    这两个字段一律由服务端裁决。未传的可选字段保留上次的值。
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sampled_at: datetime = Field(
        description="采集时刻；ISO 8601，可带 Z 或时区偏移，无后缀按 UTC 解释"
    )
    connected: bool = Field(description="APP 与录音卡当前是否保持蓝牙连接")
    battery_level: Optional[int] = Field(
        default=None, ge=0, le=100, description="电量百分比；不传保留旧值，传 null 表示本次读不到"
    )
    charging: Optional[bool] = Field(default=None, description="不传保留旧值")
    recording: Optional[bool] = Field(default=None, description="不传保留旧值")
    storage_free_bytes: Optional[int] = Field(
        default=None, ge=0, description="剩余存储容量（字节）；不传保留旧值，传 null 清空"
    )
    storage_total_bytes: Optional[int] = Field(
        default=None, ge=0, description="总存储容量（字节）；不传保留旧值，传 null 清空"
    )
    firmware_version: Optional[str] = Field(
        default=None,
        max_length=32,
        description="APP 蓝牙实测固件版本；只读入状态快照，不改设备档案；传 null 清空",
    )
    gain: Optional[int] = Field(
        default=None, ge=1, le=3, description="录音增益档位 1-3；不传保留旧值，传 null 清空"
    )
    metrics: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "可选遥测扩展槽：扁平 JSON 对象，≤20 键 / ≤2 KB，值只允许标量或一维数组；"
            "不传保留旧值，传 null 清空，传对象整体覆盖"
        ),
    )
    last_seen_at: Optional[datetime] = Field(
        default=None, description="最近一次与设备成功通信；不传时由 connected 与 sampled_at 推导"
    )
    last_sync_at: Optional[datetime] = Field(
        default=None, description="最近一次录音文件同步完成时间；不传保留旧值"
    )

    @field_validator("firmware_version")
    @classmethod
    def _check_firmware(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("firmware_version 不能是空白字符串；读不到请传 null 或不传")
        return value

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return normalize_status_metrics(v)

    @model_validator(mode="after")
    def _check_capacity(self) -> "DeviceStatusReport":
        free = self.storage_free_bytes
        total = self.storage_total_bytes
        if free is not None and total is not None and free > total:
            raise ValueError("storage_free_bytes 不能大于 storage_total_bytes")
        return self


class DeviceStatusAck(DeviceStatusOut):
    """上报响应：服务端裁决后的状态快照 + 校时与心跳节奏。"""

    server_time: datetime
    heartbeat_interval_seconds: int


class CredentialOut(BaseModel):
    """设备凭证信息（不含 AuthKey 明文）。"""

    model_config = ConfigDict(from_attributes=True)

    device_id: int
    auth_key_version: int
    status: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class CredentialIssuedOut(BaseModel):
    """凭证签发结果，包含新 AuthKey 明文。"""

    device_id: int
    auth_key: str = Field(description="新签发的 AuthKey 明文")
    auth_key_version: int
    issued_at: datetime
    expires_at: Optional[datetime] = None


class DeviceOut(BaseModel):
    """设备详情。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    sn: str
    bluetooth_name: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    hardware_version: Optional[str] = None
    firmware_version: Optional[str] = None
    production_batch: Optional[str] = None
    lifecycle_status: LifecycleStatus
    activated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # 关联信息
    assigned: bool = Field(default=False, description="当前是否处于领用中")
    assigned_user_id: Optional[str] = None
    assigned_at: Optional[datetime] = None
    bound: bool = Field(default=False, description="当前是否存在有效 APP 绑定")
    bound_user_id: Optional[str] = None
    nickname: Optional[str] = None
    status: Optional[DeviceStatusOut] = None


class MyDeviceOut(DeviceOut):
    """员工领用列表中的设备信息。"""

    auth_key: Optional[str] = Field(
        description="当前有效的 AuthKey 明文，缺少可用凭证时为 null"
    )


class DeviceListItem(DeviceOut):
    """设备列表项（在详情基础上补充当前绑定信息）。"""

    binding_status: Optional[str] = None


class DeviceCreatedOut(DeviceOut):
    """设备入库结果，包含系统生成的 AuthKey 明文。"""

    auth_key: Optional[str] = Field(default=None, description="系统生成的 AuthKey 明文")


class DeviceEventOut(BaseModel):
    """设备事件（device_event 表）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    event_type: str
    operator_id: Optional[str] = None
    event_data: Optional[dict] = None
    created_at: datetime
