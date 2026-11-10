"""APP 绑定、解绑与昵称服务（README §5.3 / §7.6）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Collection, Optional, Sequence, Set, Union

from sqlalchemy.orm import Session

from app.core.enums import (
    BINDABLE_LIFECYCLE_STATUSES,
    BindingStatus,
    EventType,
    LifecycleStatus,
)
from app.core.errors import ConflictError, DeviceStateError, PermissionDeniedError
from app.db.models import DeviceBinding
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


def _normalize_refs(user_refs: Union[str, Collection[str]]) -> Set[str]:
    """把当前登录身份规整为可比对的用户标识集合（UUID + 用户名/工号）。

    领用记录里的 ``user_id`` 可能是 Casdoor UUID，也可能是管理员填写的用户名。
    服务层统一按集合比对，并始终以领用记录中的值写入绑定关系，
    保证 ``device_binding`` 的组合外键 ``(assignment_id, device_id, user_id)`` 成立。
    """
    if isinstance(user_refs, str):
        return {user_refs.strip()} if user_refs.strip() else set()
    return {str(v).strip() for v in user_refs if v and str(v).strip()}


def bind_device(
    db: Session,
    device_id: int,
    *,
    user_refs: Union[str, Collection[str]],
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    nickname: Optional[str] = None,
    sn: Optional[str] = None,
) -> DeviceBinding:
    """员工通过 APP 绑定设备（README §5.3）。

    校验链：设备生命周期允许 → 设备处于领用中 → 申请人就是领用员工 → 无有效绑定。
    首次绑定时激活设备（registered → active）。
    """
    refs = _normalize_refs(user_refs)
    device = get_device_row(db, device_id, tenant_scope)

    if sn and sn.strip() and sn.strip() != device.sn:
        raise ConflictError("设备 SN 与绑定目标不一致")

    if LifecycleStatus(device.lifecycle_status) not in BINDABLE_LIFECYCLE_STATUSES:
        raise DeviceStateError(
            f"设备当前状态 {device.lifecycle_status} 不允许绑定（README §7.8）"
        )

    assignment = load_current_assignment(db, device.id)
    if assignment is None:
        raise PermissionDeniedError("设备尚未分配给任何员工，无法绑定")
    if assignment.user_id not in refs:
        raise PermissionDeniedError("只有当前领用员工可以绑定该设备")

    existing = load_active_binding(db, device.id)
    if existing is not None:
        if existing.user_id in refs:
            raise ConflictError("该设备已处于绑定状态")
        raise ConflictError("该设备已被绑定")

    with transaction(db):
        binding = DeviceBinding(
            device_id=device.id,
            assignment_id=assignment.id,
            # 与领用记录保持一致，否则 (assignment_id, device_id, user_id) 组合外键不成立
            user_id=assignment.user_id,
            nickname=nickname,
            status=BindingStatus.ACTIVE.value,
            bound_at=_utcnow(),
        )
        db.add(binding)
        db.flush()

        first_activation = False
        if device.activated_at is None:
            device.activated_at = _utcnow()
            if device.lifecycle_status == LifecycleStatus.REGISTERED.value:
                device.lifecycle_status = LifecycleStatus.ACTIVE.value
            first_activation = True
            db.flush()
            record_event(
                db,
                device.id,
                EventType.DEVICE_ACTIVATED,
                operator_id=operator_id,
                event_data={"sn": device.sn, "user_id": binding.user_id},
            )

        record_event(
            db,
            device.id,
            EventType.DEVICE_BOUND,
            operator_id=operator_id,
            event_data={
                "binding_id": binding.id,
                "assignment_id": assignment.id,
                "user_id": binding.user_id,
                "nickname": nickname,
                "first_activation": first_activation,
            },
        )

    return binding


def unbind_device(
    db: Session,
    device_id: int,
    *,
    user_refs: Union[str, Collection[str]],
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    is_admin: bool = False,
    reason: Optional[str] = None,
) -> DeviceBinding:
    """解除 APP 绑定（README §5.3 步骤 / §7.7）。

    员工可解绑自己的设备；管理员可撤销任意绑定（README §7.7）。
    解绑后保留历史记录，录音数据不随之删除（README §1 核心业务原则）。
    """
    refs = _normalize_refs(user_refs)
    device = get_device_row(db, device_id, tenant_scope)
    binding = load_active_binding(db, device.id)
    if binding is None:
        raise ConflictError("该设备当前没有有效绑定")

    if binding.user_id not in refs and not is_admin:
        raise PermissionDeniedError("只能解绑自己的设备，或由管理员撤销绑定")

    with transaction(db):
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
                "reason": reason or ("admin_revoke" if is_admin else "self_unbind"),
                "by_admin": is_admin,
            },
        )

    return binding


def update_nickname(
    db: Session,
    device_id: int,
    *,
    nickname: str,
    user_refs: Union[str, Collection[str]],
    tenant_scope: Optional[Sequence[str]],
    operator_id: Optional[str] = None,
    is_admin: bool = False,
) -> DeviceBinding:
    """修改员工为自己领用设备设置的昵称（README §6 PUT /devices/{id}/nickname）。

    昵称属于绑定关系，不属于设备主表（README §3.4）。
    """
    refs = _normalize_refs(user_refs)
    device = get_device_row(db, device_id, tenant_scope)
    binding = load_active_binding(db, device.id)
    if binding is None:
        raise ConflictError("该设备当前没有有效绑定，无法设置昵称")

    if binding.user_id not in refs and not is_admin:
        raise PermissionDeniedError("只能修改自己设备的昵称")

    with transaction(db):
        old_nickname = binding.nickname
        binding.nickname = nickname
        db.flush()
        record_event(
            db,
            device.id,
            EventType.NICKNAME_UPDATED,
            operator_id=operator_id,
            event_data={
                "binding_id": binding.id,
                "user_id": binding.user_id,
                "from": old_nickname,
                "to": nickname,
            },
        )

    return binding
