"""员工自助接口：查看自己领用与绑定的设备。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.db.models import Device, DeviceAssignment, DeviceCredential
from app.db.session import get_db
from app.schemas.common import PageQuery, PageResult
from app.schemas.device import DeviceOut, MyDeviceOut
from app.services import device_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["我的设备"])


def _my_devices_stmt(user: CurrentUser):
    """当前用户「领用中」的设备查询。

    业务表 ``user_id`` 列的规范值是工号（``preferred_username``）。
    ``user_refs`` 同时纳入技术 UUID，只为兼容按 UUID 写入的历史记录；
    姓名（display_name）不参与匹配。
    租户范围仍严格取服务端推导的 ``user.tenant_id``，不采信客户端传值。
    """
    return (
        select(Device)
        .join(DeviceAssignment, DeviceAssignment.device_id == Device.id)
        .where(
            DeviceAssignment.user_id.in_(user.user_refs),
            DeviceAssignment.returned_at.is_(None),
            DeviceAssignment.tenant_id == user.tenant_id,
            Device.tenant_id == user.tenant_id,
        )
    )


@router.get(
    "/my/devices",
    response_model=PageResult[MyDeviceOut],
    summary="我领用的设备",
    description="返回当前用户领用中的设备（含已绑定）及其当前有效的 AuthKey。",
)
async def my_devices(
    response: Response,
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> PageResult[MyDeviceOut]:
    base = _my_devices_stmt(user)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        base.add_columns(DeviceCredential.auth_key)
        .outerjoin(
            DeviceCredential,
            (DeviceCredential.active_device_id == Device.id)
            & or_(DeviceCredential.expires_at.is_(None), DeviceCredential.expires_at > now),
        )
        .order_by(Device.id.desc())
        .limit(query.limit)
        .offset(query.offset)
    )
    rows = db.execute(stmt).unique().all()
    items = [
        MyDeviceOut(**device_service.build_device_out(device), auth_key=auth_key)
        for device, auth_key in rows
    ]
    response.headers["Cache-Control"] = "no-store"
    return PageResult[MyDeviceOut].build(items, total, query)


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
        "注意三个标识的区别：user_id 是 Casdoor 技术 UUID；username / "
        "preferred_username / employee_no 是工号（业务侧员工唯一标识，与领用记录的 "
        "user_id 对应）；display_name 是姓名，仅用于展示、禁止用于身份匹配。"
        "若 is_admin 为 false，说明当前用户未命中任何管理员判定条件，"
        "详情见 admin_policy 与 admin_reason。"
    ),
)
async def my_profile(user: CurrentUser = Depends(get_current_user)) -> dict:
    identity = user.identity
    return {
        "user_id": identity.user_id,
        "username": identity.username,
        "preferred_username": identity.username,
        "employee_no": identity.employee_no,
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
