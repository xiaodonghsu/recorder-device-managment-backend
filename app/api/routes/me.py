"""员工自助接口：查看自己领用与绑定的设备。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.db.models import Device, DeviceAssignment
from app.db.session import get_db
from app.schemas.common import PageQuery, PageResult
from app.schemas.device import DeviceOut
from app.services import device_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["我的设备"])


def _my_devices_stmt(user: CurrentUser):
    """当前用户「领用中」的设备查询。

    ``user_refs`` 同时包含 Casdoor 用户 UUID 与用户名：领用记录可能存的是
    工号（管理员手工填写且后端未配置客户端凭据时无法反查为 UUID），
    只按 Token 的 UUID 精确比较会把这类记录全部漏掉。
    租户范围仍严格取服务端推导的 ``user.tenant_id``，不采信客户端传值。
    """
    return (
        select(Device)
        .join(DeviceAssignment, DeviceAssignment.device_id == Device.id)
        .where(
            DeviceAssignment.user_id.in_(user.user_refs),
            DeviceAssignment.returned_at.is_(None),
            Device.tenant_id == user.tenant_id,
        )
    )


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
    base = _my_devices_stmt(user)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = (
        db.execute(base.order_by(Device.id.desc()).limit(query.limit).offset(query.offset))
        .unique()
        .scalars()
        .all()
    )
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
            DeviceAssignment.user_id.in_(user.user_refs),
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
    description=(
        "返回 Casdoor 解析出的身份信息与管理员判定依据，便于 APP 展示与联调排查。"
        "若 is_admin 为 false，说明当前用户未命中任何管理员判定条件，"
        "详情见 admin_policy 与 admin_reason。"
    ),
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
        "roles": identity.roles,
        "groups": identity.groups,
        "permissions": identity.permissions,
        "is_admin": identity.is_admin,
        "is_global_admin": identity.is_global_admin,
        "admin_reason": identity.admin_reason,
        "auth_method": identity.auth_method,
        "admin_policy": {
            "admin_roles": sorted(settings.admin_roles),
            "admin_groups": sorted(settings.admin_groups),
            "admin_permissions": sorted(settings.admin_permissions),
            "admin_users": sorted(settings.admin_users),
            "global_admin_roles": sorted(settings.global_admin_roles),
        },
    }
