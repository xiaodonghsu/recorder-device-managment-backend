"""设备领用与归还服务（README §5.2 / §5.4）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.core.enums import (
    ASSIGNABLE_LIFECYCLE_STATUSES,
    BindingStatus,
    EventType,
    LifecycleStatus,
)
from app.core.errors import ConflictError, DeviceStateError, PermissionDeniedError
from app.db.models import DeviceAssignment
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


def assign_device(
    db: Session,
    device_id: int,
    *,
    user_id: str,
    tenant_id: str,
    operator_id: Optional[str] = None,
    note: Optional[str] = None,
) -> DeviceAssignment:
    """分配设备给员工（README §5.2）。

    校验链：设备属于目标企业 → 生命周期允许领用 → 无其他有效领用记录。
    """
    device = get_device_row(db, device_id, [tenant_id])

    if LifecycleStatus(device.lifecycle_status) not in ASSIGNABLE_LIFECYCLE_STATUSES:
        raise DeviceStateError(
            f"设备当前状态 {device.lifecycle_status} 不允许领用"
        )

    current = load_current_assignment(db, device.id)
    if current is not None:
        raise ConflictError("该设备已被领用，请先归还")

    with transaction(db):
        assignment = DeviceAssignment(
            device_id=device.id,
            tenant_id=tenant_id,
            user_id=user_id,
            assigned_by=operator_id,
            assigned_at=_utcnow(),
        )
        db.add(assignment)
        db.flush()

        record_event(
            db,
            device.id,
            EventType.DEVICE_ASSIGNED,
            operator_id=operator_id,
            event_data={
                "assignment_id": assignment.id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "note": note,
            },
        )

    return assignment


def return_device(
    db: Session,
    device_id: int,
    *,
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    reason: Optional[str] = None,
    revoke_binding: bool = True,
) -> DeviceAssignment:
    """设备归还（README §5.4）。

    流程：解除 APP 绑定 → 关闭领用记录 → 记录原因 → 设备回到库存 → 记录事件。
    归还后原员工立即失去设备操作权限（README §7.9）。
    """
    device = get_device_row(db, device_id, tenant_scope)
    assignment = load_current_assignment(db, device.id)
    if assignment is None:
        raise ConflictError("该设备当前未被领用，无法归还")

    with transaction(db):
        binding = load_active_binding(db, device.id)
        if binding is not None and revoke_binding:
            binding.status = BindingStatus.UNBOUND.value
            binding.unbound_at = _utcnow()
            db.flush()
            record_event(
                db,
                device.id,
                EventType.DEVICE_UNBOUND,
                operator_id=operator_id,
                event_data={
                    "binding_id": binding.id,
                    "user_id": binding.user_id,
                    "nickname": binding.nickname,
                    "reason": "device_returned",
                },
            )

        assignment.returned_at = _utcnow()
        assignment.return_reason = reason
        db.flush()

        record_event(
            db,
            device.id,
            EventType.DEVICE_RETURNED,
            operator_id=operator_id,
            event_data={
                "assignment_id": assignment.id,
                "user_id": assignment.user_id,
                "reason": reason,
                "binding_revoked": bool(binding is not None and revoke_binding),
            },
        )

    return assignment


def get_assignment_for_device(db: Session, device_id: int) -> Optional[DeviceAssignment]:
    return load_current_assignment(db, device_id)


def assert_holder(
    db: Session,
    device_id: int,
    user_id: str,
    *,
    is_admin: bool = False,
) -> DeviceAssignment:
    """校验当前用户是否为该设备的领用员工（README §7.6）。"""
    assignment = load_current_assignment(db, device_id)
    if assignment is None:
        raise PermissionDeniedError("设备未被领用，无法操作")
    if assignment.user_id != user_id and not is_admin:
        raise PermissionDeniedError("只有当前领用员工可以执行该操作")
    return assignment
