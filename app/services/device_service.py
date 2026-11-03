"""设备档案服务：入库、批量入库、查询、档案更新、生命周期流转。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.enums import (
    LIFECYCLE_TRANSITIONS,
    PROFILE_EDITABLE_STATUSES,
    CredentialStatus,
    EventType,
    LifecycleStatus,
)
from app.core.errors import (
    ConflictError,
    DeviceStateError,
    NotFoundError,
    ValidationError,
)
from app.core.security import generate_auth_key, hash_auth_key
from app.db.models import (
    Device,
    DeviceAssignment,
    DeviceBinding,
    DeviceCredential,
    DeviceEvent,
    DeviceStatus,
)
from app.schemas.common import PageQuery, PageResult
from app.schemas.device import (
    DeviceBatchCreate,
    DeviceBatchResult,
    DeviceBatchResultItem,
    DeviceCreate,
    DeviceUpdate,
)
from app.services.event_service import record_event
from app.services.transaction import transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _credential_expiry() -> Optional[datetime]:
    days = settings.auth_key_default_ttl_days
    if days <= 0:
        return None
    return _utcnow() + timedelta(days=days)


def _assert_tenant(device: Device, tenant_scope: Optional[Sequence[str]]) -> None:
    """租户隔离校验。

    tenant_scope 为 None 表示不限制（跨组织管理员）；否则设备必须属于列表内的企业。
    越权一律按「资源不存在」处理，避免泄露其他企业的设备是否存在。
    """
    if tenant_scope is None:
        return
    if device.tenant_id not in tenant_scope:
        raise NotFoundError("设备不存在")


def get_device_row(
    db: Session,
    device_id: int,
    tenant_scope: Optional[Sequence[str]],
    *,
    with_status: bool = True,
) -> Device:
    """按主键获取设备并做租户校验。"""
    stmt = select(Device).where(Device.id == device_id)
    if with_status:
        stmt = stmt.options(joinedload(Device.status))
    device = db.execute(stmt).unique().scalar_one_or_none()
    if device is None:
        raise NotFoundError("设备不存在")
    _assert_tenant(device, tenant_scope)
    return device


def get_device_by_sn(db: Session, sn: str) -> Optional[Device]:
    return db.execute(select(Device).where(Device.sn == sn)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 设备入库（README §5.1）
# ---------------------------------------------------------------------------
def register_device(
    db: Session,
    payload: DeviceCreate,
    *,
    tenant_id: str,
    operator_id: Optional[str] = None,
) -> Tuple[Device, Optional[str]]:
    """设备入库。

    流程：录入档案 → 生成或导入 AuthKey → 保存摘要 → 创建 device /
    device_credential / device_status → 记录 device_registered 事件。

    返回 (设备, AuthKey 明文)。AuthKey 明文仅在系统生成时返回一次，
    数据库只保存 HMAC 摘要。
    """
    existing = get_device_by_sn(db, payload.sn)
    if existing is not None:
        raise ConflictError(f"SN 已存在：{payload.sn}")

    with transaction(db):
        device = Device(
            tenant_id=tenant_id,
            sn=payload.sn,
            bluetooth_name=payload.bluetooth_name,
            model=payload.model,
            manufacturer=payload.manufacturer,
            hardware_version=payload.hardware_version,
            firmware_version=payload.firmware_version,
            production_batch=payload.production_batch,
            lifecycle_status=LifecycleStatus.REGISTERED.value,
        )
        db.add(device)
        db.flush()

        plaintext = payload.auth_key or generate_auth_key()
        credential = DeviceCredential(
            device_id=device.id,
            auth_key_hash=hash_auth_key(plaintext),
            auth_key_version=1,
            status=CredentialStatus.ACTIVE.value,
            issued_at=_utcnow(),
            expires_at=_credential_expiry(),
        )
        db.add(credential)
        db.add(DeviceStatus(device_id=device.id, online=False, charging=False, recording=False))
        db.flush()

        record_event(
            db,
            device.id,
            EventType.DEVICE_REGISTERED,
            operator_id=operator_id,
            event_data={
                "sn": device.sn,
                "tenant_id": device.tenant_id,
                "model": device.model,
                "manufacturer": device.manufacturer,
                "production_batch": device.production_batch,
                "auth_key_source": "imported" if payload.auth_key else "generated",
                "auth_key_version": credential.auth_key_version,
            },
        )

    return device, (None if payload.auth_key else plaintext)


def register_devices_batch(
    db: Session,
    payload: DeviceBatchCreate,
    *,
    tenant_id: str,
    operator_id: Optional[str] = None,
) -> DeviceBatchResult:
    """批量入库（README §8 P0）。

    每台设备独立事务，单条失败不影响其余设备（continue_on_error=True 时）。
    """
    items: List[DeviceBatchResultItem] = []
    succeeded = 0

    for item in payload.items:
        try:
            device, plaintext = register_device(
                db, item, tenant_id=tenant_id, operator_id=operator_id
            )
            succeeded += 1
            items.append(
                DeviceBatchResultItem(
                    sn=item.sn,
                    success=True,
                    device_id=device.id,
                    auth_key=plaintext,
                )
            )
        except (ConflictError, ValidationError, DeviceStateError) as exc:
            if not payload.continue_on_error:
                raise
            db.rollback()
            items.append(
                DeviceBatchResultItem(sn=item.sn, success=False, error=exc.message)
            )
        except IntegrityError as exc:
            if not payload.continue_on_error:
                raise
            db.rollback()
            logger.warning("批量入库唯一约束冲突: %s", exc)
            items.append(
                DeviceBatchResultItem(sn=item.sn, success=False, error="SN 已存在或数据冲突")
            )

    return DeviceBatchResult(
        total=len(payload.items),
        succeeded=succeeded,
        failed=len(payload.items) - succeeded,
        items=items,
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def list_devices(
    db: Session,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    keyword: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
    model: Optional[str] = None,
    manufacturer: Optional[str] = None,
    assigned: Optional[bool] = None,
    bound: Optional[bool] = None,
    user_id: Optional[str] = None,
) -> PageResult:
    """设备列表（README §6 GET /api/devices）。"""
    stmt = select(Device).options(joinedload(Device.status))

    if tenant_scope is not None:
        stmt = stmt.where(Device.tenant_id.in_(list(tenant_scope)))
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(Device.sn.like(like), Device.bluetooth_name.like(like), Device.model.like(like))
        )
    if lifecycle_status:
        stmt = stmt.where(Device.lifecycle_status == lifecycle_status)
    if model:
        stmt = stmt.where(Device.model == model)
    if manufacturer:
        stmt = stmt.where(Device.manufacturer == manufacturer)

    if assigned is not None or bound is not None or user_id:
        current_assignment = (
            select(DeviceAssignment.id)
            .where(
                DeviceAssignment.device_id == Device.id,
                DeviceAssignment.returned_at.is_(None),
            )
            .correlate(Device)
        )
        active_binding = (
            select(DeviceBinding.id)
            .where(DeviceBinding.device_id == Device.id, DeviceBinding.status == "active")
            .correlate(Device)
        )
        if assigned is True:
            stmt = stmt.where(current_assignment.exists())
        elif assigned is False:
            stmt = stmt.where(~current_assignment.exists())
        if bound is True:
            stmt = stmt.where(active_binding.exists())
        elif bound is False:
            stmt = stmt.where(~active_binding.exists())
        if user_id:
            stmt = stmt.where(
                select(DeviceAssignment.id)
                .where(
                    DeviceAssignment.device_id == Device.id,
                    DeviceAssignment.returned_at.is_(None),
                    DeviceAssignment.user_id == user_id,
                )
                .correlate(Device)
                .exists()
            )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()

    rows = (
        db.execute(
            stmt.order_by(Device.id.desc()).limit(query.limit).offset(query.offset)
        )
        .unique()
        .scalars()
        .all()
    )

    return PageResult.build([build_device_out(row) for row in rows], total, query)


def build_device_out(device: Device) -> Dict[str, Any]:
    """组装设备输出：附加当前领用、当前绑定与状态信息。"""
    assignment = get_current_assignment(device.id, device)
    binding = get_active_binding(device.id, device)
    return {
        "id": device.id,
        "tenant_id": device.tenant_id,
        "sn": device.sn,
        "bluetooth_name": device.bluetooth_name,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "hardware_version": device.hardware_version,
        "firmware_version": device.firmware_version,
        "production_batch": device.production_batch,
        "lifecycle_status": device.lifecycle_status,
        "activated_at": device.activated_at,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
        "assigned": assignment is not None,
        "assigned_user_id": assignment.user_id if assignment else None,
        "assigned_at": assignment.assigned_at if assignment else None,
        "bound": binding is not None,
        "bound_user_id": binding.user_id if binding else None,
        "nickname": binding.nickname if binding else None,
        "binding_status": binding.status if binding else None,
        "status": device.status,
    }


# ---------------------------------------------------------------------------
# 当前领用 / 当前绑定（利用 generated column 语义）
# ---------------------------------------------------------------------------
def get_current_assignment(
    device_id: int, device: Optional[Device] = None
) -> Optional[DeviceAssignment]:
    """取当前有效领用记录（returned_at IS NULL）。"""
    if device is not None and device.assignments:
        for item in device.assignments:
            if item.returned_at is None:
                return item
    return None


def get_active_binding(device_id: int, device: Optional[Device] = None) -> Optional[DeviceBinding]:
    """取当前有效 APP 绑定（status = active）。"""
    if device is not None and device.bindings:
        for item in device.bindings:
            if item.status == "active":
                return item
    return None


def load_current_assignment(db: Session, device_id: int) -> Optional[DeviceAssignment]:
    return db.execute(
        select(DeviceAssignment).where(
            DeviceAssignment.device_id == device_id,
            DeviceAssignment.returned_at.is_(None),
        )
    ).scalar_one_or_none()


def load_active_binding(db: Session, device_id: int) -> Optional[DeviceBinding]:
    return db.execute(
        select(DeviceBinding).where(
            DeviceBinding.device_id == device_id,
            DeviceBinding.status == "active",
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 档案更新
# ---------------------------------------------------------------------------
def update_device_profile(
    db: Session,
    device_id: int,
    payload: DeviceUpdate,
    *,
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
) -> Device:
    """更新设备档案（SN 不可修改，README §7.1）。"""
    device = get_device_row(db, device_id, tenant_scope)

    if device.lifecycle_status not in PROFILE_EDITABLE_STATUSES:
        raise DeviceStateError(f"设备已{device.lifecycle_status}，档案不可修改")

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        return device

    with transaction(db):
        for field, value in changes.items():
            setattr(device, field, value)
        db.flush()
        record_event(
            db,
            device.id,
            EventType.DEVICE_PROFILE_UPDATED,
            operator_id=operator_id,
            event_data={"fields": sorted(changes.keys()), "values": changes},
        )
    return device


# ---------------------------------------------------------------------------
# 生命周期流转
# ---------------------------------------------------------------------------
def change_lifecycle(
    db: Session,
    device_id: int,
    target: LifecycleStatus,
    *,
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Device:
    """变更设备生命周期状态，并记录对应事件。"""
    device = get_device_row(db, device_id, tenant_scope)
    current = LifecycleStatus(device.lifecycle_status)

    if current == target:
        raise ValidationError(f"设备已处于 {target.value} 状态")
    if target not in LIFECYCLE_TRANSITIONS.get(current, set()):
        raise DeviceStateError(f"不允许从 {current.value} 流转到 {target.value}")

    event_map = {
        LifecycleStatus.ACTIVE: EventType.DEVICE_ENABLED,
        LifecycleStatus.DISABLED: EventType.DEVICE_DISABLED,
        LifecycleStatus.LOST: EventType.DEVICE_LOST,
        LifecycleStatus.REPAIR: EventType.DEVICE_REPAIRED,
        LifecycleStatus.RETIRED: EventType.DEVICE_RETIRED,
    }

    with transaction(db):
        device.lifecycle_status = target.value
        if target == LifecycleStatus.ACTIVE and device.activated_at is None:
            device.activated_at = _utcnow()
        db.flush()
        record_event(
            db,
            device.id,
            event_map.get(target, EventType.DEVICE_PROFILE_UPDATED),
            operator_id=operator_id,
            event_data={
                "from": current.value,
                "to": target.value,
                "reason": reason,
            },
        )
    return device


# ---------------------------------------------------------------------------
# 历史查询
# ---------------------------------------------------------------------------
def list_device_events(
    db: Session,
    device_id: int,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    event_type: Optional[str] = None,
) -> PageResult:
    """设备历史事件（README §6 GET /api/devices/{id}/history）。"""
    device = get_device_row(db, device_id, tenant_scope, with_status=False)

    stmt = select(DeviceEvent).where(DeviceEvent.device_id == device.id)
    if event_type:
        stmt = stmt.where(DeviceEvent.event_type == event_type)

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(DeviceEvent.id.desc()).limit(query.limit).offset(query.offset))
        .scalars()
        .all()
    )
    return PageResult.build(
        [
            {
                "id": row.id,
                "device_id": row.device_id,
                "event_type": row.event_type,
                "operator_id": row.operator_id,
                "event_data": row.event_data,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        total,
        query,
    )


def list_device_assignments(
    db: Session,
    device_id: int,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
) -> PageResult:
    """设备领用历史。"""
    device = get_device_row(db, device_id, tenant_scope, with_status=False)
    stmt = select(DeviceAssignment).where(DeviceAssignment.device_id == device.id)
    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(DeviceAssignment.id.desc()).limit(query.limit).offset(query.offset))
        .scalars()
        .all()
    )
    return PageResult.build(
        [
            {
                "id": row.id,
                "device_id": row.device_id,
                "tenant_id": row.tenant_id,
                "user_id": row.user_id,
                "assigned_by": row.assigned_by,
                "assigned_at": row.assigned_at,
                "returned_at": row.returned_at,
                "return_reason": row.return_reason,
                "created_at": row.created_at,
                "device_sn": device.sn,
            }
            for row in rows
        ],
        total,
        query,
    )


def list_device_bindings(
    db: Session,
    device_id: int,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
) -> PageResult:
    """设备绑定历史（含历史昵称）。"""
    device = get_device_row(db, device_id, tenant_scope, with_status=False)
    stmt = select(DeviceBinding).where(DeviceBinding.device_id == device.id)
    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(DeviceBinding.id.desc()).limit(query.limit).offset(query.offset))
        .scalars()
        .all()
    )
    return PageResult.build(
        [
            {
                "id": row.id,
                "device_id": row.device_id,
                "assignment_id": row.assignment_id,
                "user_id": row.user_id,
                "nickname": row.nickname,
                "status": row.status,
                "bound_at": row.bound_at,
                "unbound_at": row.unbound_at,
                "created_at": row.created_at,
                "device_sn": device.sn,
                "device_model": device.model,
            }
            for row in rows
        ],
        total,
        query,
    )
