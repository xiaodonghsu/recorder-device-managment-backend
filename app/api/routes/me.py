"""员工自助接口：查看自己领用与绑定的设备。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.db.models import Device, DeviceAssignment
from app.db.session import get_db
from app.schemas.common import PageQuery, PageResult
from app.schemas.device import DeviceOut
from app.services import device_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["我的设备"])


@router.get(
    "/my/devices",
    response_model=PageResult[DeviceOut],
    summary="我领用的设备",
    description="返回当前用户领用中（含已绑定）的设备列表。",
)
async def my_devices(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> PageResult:
    stmt = (
        select(Device)
        .join(DeviceAssignment, DeviceAssignment.device_id == Device.id)
        .options()
        .where(
            DeviceAssignment.user_id == user.user_id,
            DeviceAssignment.returned_at.is_(None),
            Device.tenant_id == user.tenant_id,
        )
    )
    rows = (
        db.execute(stmt.order_by(Device.id.desc()).limit(query.limit).offset(query.offset))
        .unique()
        .scalars()
        .all()
    )
    total = len(rows)
    return PageResult.build([DeviceOut(**device_service.build_device_out(row)) for row in rows], total, query)


@router.get(
    "/my/devices/{device_id}",
    response_model=DeviceOut,
    summary="我的设备详情",
)
async def my_device(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> DeviceOut:
    assignment = db.execute(
        select(DeviceAssignment).where(
            DeviceAssignment.device_id == device_id,
            DeviceAssignment.user_id == user.user_id,
            DeviceAssignment.returned_at.is_(None),
            DeviceAssignment.tenant_id == user.tenant_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("设备不存在或不在您的领用清单中")

    device = device_service.get_device_row(db, device_id, [user.tenant_id])
    return DeviceOut(**device_service.build_device_out(device))


@router.get(
    "/my/profile",
    summary="当前登录身份",
    description="返回 Casdoor 解析出的身份信息，便于 APP 展示与联调。",
)
async def my_profile(user: CurrentUser = Depends(get_current_user)) -> dict:
    identity = user.identity
    return {
        "user_id": identity.user_id,
        "username": identity.username,
        "display_name": identity.display_name,
        "tenant_id": identity.tenant_id,
        "email": identity.email,
        "phone": identity.phone,
        "role": identity.role,
        "groups": identity.groups,
        "roles": identity.roles,
        "is_admin": identity.is_admin,
        "is_global_admin": identity.is_global_admin,
        "auth_method": identity.auth_method,
    }
