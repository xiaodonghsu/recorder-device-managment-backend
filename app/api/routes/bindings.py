"""APP 绑定 / 解绑 / 昵称接口（README §6）。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.core.errors import PermissionDeniedError
from app.db.session import get_db
from app.schemas.binding import BindingCreate, BindingOut, NicknameUpdate
from app.schemas.common import PageQuery, PageResult
from app.services import device_service
from app.services.binding_service import bind_device, unbind_device, update_nickname

logger = logging.getLogger(__name__)

router = APIRouter(tags=["APP 绑定"])


@router.post(
    "/devices/{device_id}/bind",
    response_model=BindingOut,
    summary="APP 绑定设备",
    description=(
        "员工在 APP 中绑定自己领用的设备。只有当前领用员工可以绑定；"
        "禁用、挂失、报废的设备不得创建新绑定。"
    ),
)
async def bind(
    payload: BindingCreate,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> BindingOut:
    device = device_service.get_device_row(db, device_id, user.tenant_scope)
    if device.tenant_id != user.tenant_id:
        raise PermissionDeniedError("不能绑定其它企业的设备")

    binding = bind_device(
        db,
        device_id,
        user_refs=user.user_refs,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        nickname=payload.nickname,
        sn=payload.sn,
    )
    return BindingOut(
        id=binding.id,
        device_id=binding.device_id,
        assignment_id=binding.assignment_id,
        user_id=binding.user_id,
        nickname=binding.nickname,
        status=binding.status,
        bound_at=binding.bound_at,
        unbound_at=binding.unbound_at,
        created_at=binding.created_at,
        device_sn=device.sn,
        device_model=device.model,
    )


@router.post(
    "/devices/{device_id}/unbind",
    response_model=BindingOut,
    summary="APP 解绑设备",
    description="员工解绑自己的设备；管理员可撤销任意绑定。解绑不影响录音数据。",
)
async def unbind(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="解绑原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> BindingOut:
    binding = unbind_device(
        db,
        device_id,
        user_refs=user.user_refs,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        is_admin=user.is_admin,
        reason=reason,
    )
    device = device_service.get_device_row(db, device_id, user.tenant_scope, with_status=False)
    return BindingOut(
        id=binding.id,
        device_id=binding.device_id,
        assignment_id=binding.assignment_id,
        user_id=binding.user_id,
        nickname=binding.nickname,
        status=binding.status,
        bound_at=binding.bound_at,
        unbound_at=binding.unbound_at,
        created_at=binding.created_at,
        device_sn=device.sn,
        device_model=device.model,
    )


@router.put(
    "/devices/{device_id}/nickname",
    response_model=BindingOut,
    summary="修改设备昵称",
    description="员工为自己领用的设备设置个性化昵称；昵称属于绑定关系，不属于设备主表。",
)
async def set_nickname(
    payload: NicknameUpdate,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> BindingOut:
    binding = update_nickname(
        db,
        device_id,
        nickname=payload.nickname,
        user_refs=user.user_refs,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        is_admin=user.is_admin,
    )
    device = device_service.get_device_row(db, device_id, user.tenant_scope, with_status=False)
    return BindingOut(
        id=binding.id,
        device_id=binding.device_id,
        assignment_id=binding.assignment_id,
        user_id=binding.user_id,
        nickname=binding.nickname,
        status=binding.status,
        bound_at=binding.bound_at,
        unbound_at=binding.unbound_at,
        created_at=binding.created_at,
        device_sn=device.sn,
        device_model=device.model,
    )


@router.get(
    "/devices/{device_id}/bindings",
    response_model=PageResult[BindingOut],
    summary="绑定历史",
    description="返回该设备的全部绑定记录（含历史昵称与解绑时间）。",
)
async def device_bindings(
    device_id: int = Path(ge=1),
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> PageResult:
    if not user.is_admin:
        # 员工只能查看与自己相关的绑定记录
        return _filter_bindings_for_user(db, device_id, user, query)
    return device_service.list_device_bindings(db, device_id, user.tenant_scope, query)


def _filter_bindings_for_user(
    db: Session, device_id: int, user: CurrentUser, query: PageQuery
) -> PageResult:
    """员工只能查看与自己相关的绑定记录。"""
    from sqlalchemy import func, select

    from app.db.models import DeviceBinding

    device = device_service.get_device_row(db, device_id, user.tenant_scope, with_status=False)
    stmt = select(DeviceBinding).where(
        DeviceBinding.device_id == device.id,
        DeviceBinding.user_id.in_(user.user_refs),
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
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
