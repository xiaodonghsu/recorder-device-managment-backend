"""表格级管理接口：各业务表的跨设备查询（README §3 六张表）。

与设备子资源接口（/api/devices/{id}/...）的分工：

- 设备子资源接口：单台设备的详情与状态变更；
- 本模块：**跨设备**地分页浏览领用记录、APP 绑定、设备凭证、设备状态、
  事件日志，并提供概览聚合指标。

本模块全部只读，写操作仍走设备子资源接口，以复用服务层的状态机、事务与事件记录。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_admin
from app.db.session import get_db
from app.schemas.common import PageQuery, PageResult
from app.schemas.table import (
    AssignmentRow,
    BindingRow,
    CredentialRow,
    EmployeeRow,
    EventRow,
    OverviewStats,
    StatusRow,
)
from app.services import table_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["表格管理"])


@router.get(
    "/tables/assignments",
    response_model=PageResult[AssignmentRow],
    summary="领用记录列表",
    description="跨设备分页浏览 device_assignment 表，可按领用状态、员工、SN 筛选。",
)
async def list_assignments(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    state: Optional[str] = Query(default=None, description="current=领用中 / returned=已归还"),
    user_id: Optional[str] = Query(default=None, description="按领用员工过滤"),
    keyword: Optional[str] = Query(default=None, description="SN / 员工 ID 模糊匹配"),
) -> PageResult:
    return table_service.list_assignments(
        db,
        user.tenant_scope,
        query,
        state=state,
        user_id=user_id,
        keyword=keyword,
    )


@router.get(
    "/tables/bindings",
    response_model=PageResult[BindingRow],
    summary="APP 绑定记录列表",
    description="跨设备分页浏览 device_binding 表，可按状态、员工、SN / 昵称筛选。",
)
async def list_bindings(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    status: Optional[str] = Query(default=None, description="active / unbound / revoked"),
    user_id: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="SN / 员工 ID / 昵称模糊匹配"),
) -> PageResult:
    return table_service.list_bindings(
        db,
        user.tenant_scope,
        query,
        status=status,
        user_id=user_id,
        keyword=keyword,
    )


@router.get(
    "/tables/credentials",
    response_model=PageResult[CredentialRow],
    summary="设备凭证列表",
    description="跨设备分页浏览 device_credential 表（不含 AuthKey 明文）。",
)
async def list_credentials(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    status: Optional[str] = Query(default=None, description="active / revoked"),
    device_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="SN 模糊匹配"),
) -> PageResult:
    return table_service.list_credentials(
        db,
        user.tenant_scope,
        query,
        status=status,
        device_id=device_id,
        keyword=keyword,
    )


@router.get(
    "/tables/statuses",
    response_model=PageResult[StatusRow],
    summary="设备状态列表",
    description="跨设备分页浏览 device_status 表，可按在线状态、生命周期状态筛选。",
)
async def list_statuses(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    online: Optional[bool] = Query(default=None),
    lifecycle_status: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="SN / 型号模糊匹配"),
) -> PageResult:
    return table_service.list_statuses(
        db,
        user.tenant_scope,
        query,
        online=online,
        lifecycle_status=lifecycle_status,
        keyword=keyword,
    )


@router.get(
    "/tables/events",
    response_model=PageResult[EventRow],
    summary="事件日志列表",
    description="跨设备分页浏览 device_event 表，可按事件类型、设备、操作人筛选。",
)
async def list_events(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    event_type: Optional[str] = Query(default=None),
    device_id: Optional[int] = Query(default=None),
    operator_id: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="SN / 操作人模糊匹配"),
) -> PageResult:
    return table_service.list_events(
        db,
        user.tenant_scope,
        query,
        event_type=event_type,
        device_id=device_id,
        operator_id=operator_id,
        keyword=keyword,
    )


@router.get(
    "/tables/employees",
    response_model=PageResult[EmployeeRow],
    summary="员工清单",
    description=(
        "从领用记录反推本企业的员工名单（用户 ID、领用设备数、有效绑定数）。"
        "用于领用弹窗选人；Casdoor 未配置 client 凭据时，这是唯一可用的员工来源。"
    ),
)
async def list_employees(
    query: PageQuery = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
    keyword: Optional[str] = Query(default=None, description="用户 ID 模糊匹配"),
) -> PageResult:
    return table_service.list_employees(
        db, user.tenant_scope, query, keyword=keyword
    )


@router.get(
    "/tables/overview",
    response_model=OverviewStats,
    summary="概览聚合指标",
    description="返回管理后台仪表盘所需的设备总量、生命周期分布、领用 / 绑定 / 凭证与事件统计。",
)
async def overview(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> OverviewStats:
    return table_service.overview_stats(db, user.tenant_scope)
