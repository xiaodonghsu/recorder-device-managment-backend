"""设备管理接口（README §6）。

管理员接口：设备列表 / 入库 / 批量入库 / 详情 / 档案更新 / 领用 / 归还 /
生命周期流转 / 历史查询。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    require_admin,
    resolve_employee_id,
    resolve_target_tenant,
)
from app.core.enums import LifecycleStatus
from app.db.session import get_db
from app.integrations.casdoor import CasdoorClient, get_casdoor_client
from app.schemas.assignment import AssignmentCreate, AssignmentOut, DeviceReturnRequest
from app.schemas.common import PageQuery, PageResult
from app.schemas.device import (
    DeviceBatchCreate,
    DeviceBatchResult,
    DeviceCreate,
    DeviceCreatedOut,
    DeviceEventOut,
    DeviceListItem,
    DeviceOut,
    DeviceStatusOut,
    DeviceUpdate,
)
from app.services import device_service
from app.services.assignment_service import assign_device, return_device

logger = logging.getLogger(__name__)

router = APIRouter(tags=["设备管理"])


@router.get(
    "/devices",
    response_model=PageResult[DeviceListItem],
    summary="设备列表",
    description="分页查询设备清单，可按关键字、生命周期状态、型号、领用/绑定情况筛选。",
)
async def list_devices(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    keyword: Optional[str] = Query(default=None, description="SN / 蓝牙名 / 型号 模糊匹配"),
    lifecycle_status: Optional[str] = Query(default=None, description="生命周期状态"),
    model: Optional[str] = Query(default=None),
    manufacturer: Optional[str] = Query(default=None),
    assigned: Optional[bool] = Query(default=None, description="是否处于领用中"),
    bound: Optional[bool] = Query(default=None, description="是否存在有效 APP 绑定"),
    user_id: Optional[str] = Query(default=None, description="按领用员工过滤"),
) -> PageResult:
    return device_service.list_devices(
        db,
        user.tenant_scope,
        query,
        keyword=keyword,
        lifecycle_status=lifecycle_status,
        model=model,
        manufacturer=manufacturer,
        assigned=assigned,
        bound=bound,
        user_id=user_id,
    )


@router.post(
    "/devices/batch",
    response_model=DeviceBatchResult,
    summary="设备批量入库",
    description="批量登记设备，单条失败不影响其余设备；系统生成的 AuthKey 明文仅在本次响应返回。",
)
async def batch_register_devices(
    payload: DeviceBatchCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceBatchResult:
    tenant_id = resolve_target_tenant(user, payload.items[0].tenant_id if payload.items else None)
    for item in payload.items:
        if item.tenant_id and item.tenant_id != tenant_id:
            from app.core.errors import ValidationError

            raise ValidationError("批量清单中的 tenant_id 必须一致")
    return device_service.register_devices_batch(
        db, payload, tenant_id=tenant_id, operator_id=user.user_id
    )


@router.post(
    "/devices",
    response_model=DeviceCreatedOut,
    status_code=201,
    summary="设备入库",
    description="登记设备档案、签发 AuthKey（只保存摘要）并记录 device_registered 事件。",
)
async def register_device(
    payload: DeviceCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceCreatedOut:
    tenant_id = resolve_target_tenant(user, payload.tenant_id)
    device, auth_key = device_service.register_device(
        db, payload, tenant_id=tenant_id, operator_id=user.user_id
    )
    result = device_service.build_device_out(device)
    result["auth_key"] = auth_key
    return DeviceCreatedOut(**result)


@router.get(
    "/devices/{device_id}",
    response_model=DeviceOut,
    summary="设备详情",
)
async def get_device(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.get_device_row(db, device_id, user.tenant_scope)
    return DeviceOut(**device_service.build_device_out(device))


@router.put(
    "/devices/{device_id}",
    response_model=DeviceOut,
    summary="更新设备档案",
    description="更新型号、固件版本等档案字段；SN 不允许修改。",
)
async def update_device(
    payload: DeviceUpdate,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.update_device_profile(
        db, device_id, payload, tenant_scope=user.tenant_scope, operator_id=user.user_id
    )
    return DeviceOut(**device_service.build_device_out(device))


@router.post(
    "/devices/{device_id}/assign",
    response_model=AssignmentOut,
    summary="分配设备给员工",
    description="管理员将设备分配给员工；一台设备同时只能分配给一个员工。",
)
async def assign(
    payload: AssignmentCreate,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    client: CasdoorClient = Depends(get_casdoor_client),
) -> AssignmentOut:
    tenant_id = resolve_target_tenant(user, payload.tenant_id)
    employee_id = await resolve_employee_id(client, tenant_id, payload.user_id)
    assignment = assign_device(
        db,
        device_id,
        user_id=employee_id,
        tenant_id=tenant_id,
        operator_id=user.user_id,
        note=payload.note,
    )
    device = device_service.get_device_row(db, device_id, user.tenant_scope, with_status=False)
    return AssignmentOut(
        id=assignment.id,
        device_id=assignment.device_id,
        tenant_id=assignment.tenant_id,
        user_id=assignment.user_id,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
        returned_at=assignment.returned_at,
        return_reason=assignment.return_reason,
        created_at=assignment.created_at,
        device_sn=device.sn,
    )


@router.post(
    "/devices/{device_id}/return",
    response_model=AssignmentOut,
    summary="设备归还",
    description="解除 APP 绑定并关闭领用记录，设备回到库存；原员工立即失去操作权限。",
)
async def return_back(
    payload: DeviceReturnRequest,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> AssignmentOut:
    assignment = return_device(
        db,
        device_id,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=payload.reason,
        revoke_binding=payload.revoke_binding,
    )
    device = device_service.get_device_row(db, device_id, user.tenant_scope, with_status=False)
    return AssignmentOut(
        id=assignment.id,
        device_id=assignment.device_id,
        tenant_id=assignment.tenant_id,
        user_id=assignment.user_id,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
        returned_at=assignment.returned_at,
        return_reason=assignment.return_reason,
        created_at=assignment.created_at,
        device_sn=device.sn,
    )


@router.get(
    "/devices/{device_id}/history",
    response_model=PageResult[DeviceEventOut],
    summary="设备历史",
    description="按时间倒序返回设备生命周期事件。",
)
async def device_history(
    device_id: int = Path(ge=1),
    query: PageQuery = Depends(),
    event_type: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> PageResult:
    return device_service.list_device_events(
        db, device_id, user.tenant_scope, query, event_type=event_type
    )


@router.get(
    "/devices/{device_id}/assignments",
    response_model=PageResult[AssignmentOut],
    summary="领用历史",
)
async def device_assignments(
    device_id: int = Path(ge=1),
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> PageResult:
    return device_service.list_device_assignments(db, device_id, user.tenant_scope, query)


@router.get(
    "/devices/{device_id}/status",
    response_model=DeviceStatusOut,
    summary="设备最新状态",
    description="返回设备状态（电量、充电、录音、最近通信时间）。",
)
async def device_status(
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceStatusOut:
    device = device_service.get_device_row(db, device_id, user.tenant_scope)
    status = device.status
    if status is None:
        return DeviceStatusOut(device_id=device.id, online=False, charging=False, recording=False)
    return DeviceStatusOut.model_validate(status)


# ---------------------------------------------------------------------------
# 生命周期流转（README §3.1 状态定义）
# ---------------------------------------------------------------------------
@router.post(
    "/devices/{device_id}/disable",
    response_model=DeviceOut,
    summary="禁用设备",
    description="禁用后设备不可再被绑定或分配。",
)
async def disable_device(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.change_lifecycle(
        db,
        device_id,
        LifecycleStatus.DISABLED,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=reason,
    )
    return DeviceOut(**device_service.build_device_out(device))


@router.post(
    "/devices/{device_id}/enable",
    response_model=DeviceOut,
    summary="启用设备",
    description="将设备恢复为 active 状态（用于解除禁用或维修完成）。",
)
async def enable_device(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.change_lifecycle(
        db,
        device_id,
        LifecycleStatus.ACTIVE,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=reason,
    )
    return DeviceOut(**device_service.build_device_out(device))


@router.post(
    "/devices/{device_id}/lost",
    response_model=DeviceOut,
    summary="设备挂失",
    description="挂失后设备不可再被绑定或分配。",
)
async def mark_device_lost(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.change_lifecycle(
        db,
        device_id,
        LifecycleStatus.LOST,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=reason,
    )
    return DeviceOut(**device_service.build_device_out(device))


@router.post(
    "/devices/{device_id}/repair",
    response_model=DeviceOut,
    summary="转入维修",
    description="设备送修；按 README §5.4，不删除领用历史。",
)
async def mark_device_repair(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.change_lifecycle(
        db,
        device_id,
        LifecycleStatus.REPAIR,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=reason,
    )
    return DeviceOut(**device_service.build_device_out(device))


@router.post(
    "/devices/{device_id}/retire",
    response_model=DeviceOut,
    summary="设备报废",
    description="报废为终态，档案锁定，不可再流转。",
)
async def retire_device(
    device_id: int = Path(ge=1),
    reason: Optional[str] = Query(default=None, description="原因"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> DeviceOut:
    device = device_service.change_lifecycle(
        db,
        device_id,
        LifecycleStatus.RETIRED,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reason=reason,
    )
    return DeviceOut(**device_service.build_device_out(device))
