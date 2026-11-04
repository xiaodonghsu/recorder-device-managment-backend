"""表格级（跨设备）查询服务。

README §3 的 6 张表需要在管理后台中「分别管理」：领用记录、APP 绑定、
设备凭证、设备状态、事件日志都不能只靠 /api/devices/{id}/... 这类
单设备子资源接口来浏览。本模块提供跨设备的分页查询与聚合统计。

约定（与既有服务层一致）：

- 路由层只负责参数解析，查询逻辑全部落在本模块；
- 租户可见范围由 ``tenant_scope`` 决定（None = 跨组织管理员，不加限制），
  **不采信客户端提交的 tenant_id**；
- 一律以 ``device.tenant_id`` 作为企业归属的权威来源；
- 本模块**只读**，不产生任何写操作与状态变更。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_, select

from app.core.enums import ASSIGNABLE_LIFECYCLE_STATUSES, LifecycleStatus
from app.db.models import (
    Device,
    DeviceAssignment,
    DeviceBinding,
    DeviceCredential,
    DeviceEvent,
    DeviceStatus,
)
from app.schemas.common import PageQuery, PageResult
from app.schemas.table import (
    AssignmentRow,
    BindingRow,
    CredentialRow,
    EmployeeRow,
    EventRow,
    LifecycleBreakdown,
    OverviewStats,
    StatusRow,
    TopAssignee,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tenant_filter(stmt, tenant_scope: Optional[Sequence[str]], column):
    """按租户范围收敛查询。"""
    if tenant_scope is not None:
        stmt = stmt.where(column.in_(list(tenant_scope)))
    return stmt


def _count(db, stmt) -> int:
    """对带 JOIN 的查询做总数统计。"""
    return db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()


def _like(keyword: Optional[str]) -> Optional[str]:
    value = (keyword or "").strip()
    return f"%{value}%" if value else None


# ---------------------------------------------------------------------------
# device_assignment：领用记录
# ---------------------------------------------------------------------------
def list_assignments(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    state: Optional[str] = None,
    user_id: Optional[str] = None,
    keyword: Optional[str] = None,
) -> PageResult:
    """跨设备的领用记录列表。

    ``state`` 取值：``current``（领用中）/ ``returned``（已归还）。
    """
    stmt = (
        select(DeviceAssignment, Device.sn, Device.model)
        .join(Device, Device.id == DeviceAssignment.device_id)
    )
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    if state == "current":
        stmt = stmt.where(DeviceAssignment.returned_at.is_(None))
    elif state == "returned":
        stmt = stmt.where(DeviceAssignment.returned_at.is_not(None))
    if user_id:
        stmt = stmt.where(DeviceAssignment.user_id == user_id)

    like = _like(keyword)
    if like:
        stmt = stmt.where(
            or_(Device.sn.like(like), DeviceAssignment.user_id.like(like))
        )

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(DeviceAssignment.id.desc()).limit(query.limit).offset(query.offset)
    ).all()

    return PageResult.build(
        [
            AssignmentRow(
                id=assignment.id,
                device_id=assignment.device_id,
                device_sn=sn,
                device_model=model,
                tenant_id=assignment.tenant_id,
                user_id=assignment.user_id,
                assigned_by=assignment.assigned_by,
                assigned_at=assignment.assigned_at,
                returned_at=assignment.returned_at,
                return_reason=assignment.return_reason,
                created_at=assignment.created_at,
                is_current=assignment.returned_at is None,
            )
            for assignment, sn, model in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# device_binding：APP 绑定记录
# ---------------------------------------------------------------------------
def list_bindings(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    status: Optional[str] = None,
    user_id: Optional[str] = None,
    keyword: Optional[str] = None,
) -> PageResult:
    """跨设备的 APP 绑定记录列表。"""
    stmt = (
        select(DeviceBinding, Device.sn, Device.model)
        .join(Device, Device.id == DeviceBinding.device_id)
    )
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    if status:
        stmt = stmt.where(DeviceBinding.status == status)
    if user_id:
        stmt = stmt.where(DeviceBinding.user_id == user_id)

    like = _like(keyword)
    if like:
        stmt = stmt.where(
            or_(
                Device.sn.like(like),
                DeviceBinding.user_id.like(like),
                DeviceBinding.nickname.like(like),
            )
        )

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(DeviceBinding.id.desc()).limit(query.limit).offset(query.offset)
    ).all()

    return PageResult.build(
        [
            BindingRow(
                id=binding.id,
                device_id=binding.device_id,
                device_sn=sn,
                device_model=model,
                assignment_id=binding.assignment_id,
                user_id=binding.user_id,
                nickname=binding.nickname,
                status=binding.status,
                bound_at=binding.bound_at,
                unbound_at=binding.unbound_at,
                created_at=binding.created_at,
            )
            for binding, sn, model in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# device_credential：设备凭证
# ---------------------------------------------------------------------------
def list_credentials(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    status: Optional[str] = None,
    device_id: Optional[int] = None,
    keyword: Optional[str] = None,
) -> PageResult:
    """跨设备的凭证版本列表（不含 AuthKey 明文）。"""
    stmt = select(DeviceCredential, Device.sn).join(Device, Device.id == DeviceCredential.device_id)
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    if status:
        stmt = stmt.where(DeviceCredential.status == status)
    if device_id:
        stmt = stmt.where(DeviceCredential.device_id == device_id)

    like = _like(keyword)
    if like:
        stmt = stmt.where(Device.sn.like(like))

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(DeviceCredential.id.desc()).limit(query.limit).offset(query.offset)
    ).all()

    return PageResult.build(
        [
            CredentialRow(
                id=credential.id,
                device_id=credential.device_id,
                device_sn=sn,
                auth_key_version=credential.auth_key_version,
                status=credential.status,
                issued_at=credential.issued_at,
                expires_at=credential.expires_at,
                revoked_at=credential.revoked_at,
                created_at=credential.created_at,
            )
            for credential, sn in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# device_status：设备最新状态
# ---------------------------------------------------------------------------
def list_statuses(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    online: Optional[bool] = None,
    lifecycle_status: Optional[str] = None,
    keyword: Optional[str] = None,
) -> PageResult:
    """跨设备的最新状态列表。"""
    stmt = (
        select(DeviceStatus, Device.sn, Device.model, Device.tenant_id, Device.lifecycle_status)
        .join(Device, Device.id == DeviceStatus.device_id)
    )
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    if online is not None:
        stmt = stmt.where(DeviceStatus.online.is_(online))
    if lifecycle_status:
        stmt = stmt.where(Device.lifecycle_status == lifecycle_status)

    like = _like(keyword)
    if like:
        stmt = stmt.where(or_(Device.sn.like(like), Device.model.like(like)))

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(DeviceStatus.device_id.desc()).limit(query.limit).offset(query.offset)
    ).all()

    return PageResult.build(
        [
            StatusRow(
                device_id=status.device_id,
                device_sn=sn,
                device_model=model,
                tenant_id=tenant_id,
                lifecycle_status=lifecycle_status,
                online=bool(status.online),
                battery_level=status.battery_level,
                charging=bool(status.charging),
                recording=bool(status.recording),
                last_seen_at=status.last_seen_at,
                last_sync_at=status.last_sync_at,
                updated_at=status.updated_at,
            )
            for status, sn, model, tenant_id, lifecycle_status in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# device_event：生命周期事件
# ---------------------------------------------------------------------------
def list_events(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    event_type: Optional[str] = None,
    device_id: Optional[int] = None,
    operator_id: Optional[str] = None,
    keyword: Optional[str] = None,
) -> PageResult:
    """跨设备的事件日志列表。"""
    stmt = select(DeviceEvent, Device.sn).join(Device, Device.id == DeviceEvent.device_id)
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    if event_type:
        stmt = stmt.where(DeviceEvent.event_type == event_type)
    if device_id:
        stmt = stmt.where(DeviceEvent.device_id == device_id)
    if operator_id:
        stmt = stmt.where(DeviceEvent.operator_id == operator_id)

    like = _like(keyword)
    if like:
        stmt = stmt.where(or_(Device.sn.like(like), DeviceEvent.operator_id.like(like)))

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(DeviceEvent.id.desc()).limit(query.limit).offset(query.offset)
    ).all()

    return PageResult.build(
        [
            EventRow(
                id=event.id,
                device_id=event.device_id,
                device_sn=sn,
                event_type=event.event_type,
                operator_id=event.operator_id,
                event_data=event.event_data,
                created_at=event.created_at,
            )
            for event, sn in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# 员工清单（由领用 / 绑定记录反推）
# ---------------------------------------------------------------------------
def list_employees(
    db,
    tenant_scope: Optional[Sequence[str]],
    query: PageQuery,
    *,
    keyword: Optional[str] = None,
) -> PageResult:
    """从领用记录反推员工清单。

    Casdoor 的用户名反查依赖 client 凭据，未配置时无法直接列组织成员；
    本接口以「本企业已出现过领用 / 绑定记录的员工」为口径，保证领用弹窗
    在任何部署条件下都有候选名单。
    """
    stmt = (
        select(
            DeviceAssignment.user_id,
            func.count(DeviceAssignment.id).label("device_count"),
            func.max(DeviceAssignment.assigned_at).label("last_assigned_at"),
        )
        .join(Device, Device.id == DeviceAssignment.device_id)
        .group_by(DeviceAssignment.user_id)
    )
    stmt = _tenant_filter(stmt, tenant_scope, Device.tenant_id)

    like = _like(keyword)
    if like:
        stmt = stmt.where(DeviceAssignment.user_id.like(like))

    total = _count(db, stmt)
    rows = db.execute(
        stmt.order_by(func.count(DeviceAssignment.id).desc())
        .limit(query.limit)
        .offset(query.offset)
    ).all()

    # 有效绑定数单独聚合，避免与领用记录 JOIN 造成行数放大
    bind_stmt = (
        select(DeviceBinding.user_id, func.count(DeviceBinding.id))
        .join(Device, Device.id == DeviceBinding.device_id)
        .where(DeviceBinding.status == "active")
        .group_by(DeviceBinding.user_id)
    )
    bind_stmt = _tenant_filter(bind_stmt, tenant_scope, Device.tenant_id)
    binding_counts: Dict[str, int] = {
        str(user_id): int(count) for user_id, count in db.execute(bind_stmt).all()
    }

    return PageResult.build(
        [
            EmployeeRow(
                user_id=str(user_id),
                device_count=int(device_count),
                binding_count=binding_counts.get(str(user_id), 0),
                last_assigned_at=last_assigned_at,
            )
            for user_id, device_count, last_assigned_at in rows
        ],
        total,
        query,
    )


# ---------------------------------------------------------------------------
# 概览聚合
# ---------------------------------------------------------------------------
def overview_stats(db, tenant_scope: Optional[Sequence[str]]) -> OverviewStats:
    """管理后台概览指标。"""
    now = _utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # --- 设备总量与生命周期分布 ---
    device_stmt = select(Device.lifecycle_status, func.count(Device.id)).group_by(
        Device.lifecycle_status
    )
    device_stmt = _tenant_filter(device_stmt, tenant_scope, Device.tenant_id)
    lifecycle = LifecycleBreakdown()
    total_devices = 0
    for status_value, count in db.execute(device_stmt).all():
        total_devices += int(count)
        key = str(status_value)
        if hasattr(lifecycle, key):
            setattr(lifecycle, key, int(count))

    # --- 当前领用 / 当前绑定 ---
    assign_stmt = (
        select(func.count(DeviceAssignment.id))
        .join(Device, Device.id == DeviceAssignment.device_id)
        .where(DeviceAssignment.returned_at.is_(None))
    )
    assign_stmt = _tenant_filter(assign_stmt, tenant_scope, Device.tenant_id)
    assigned_devices = int(db.execute(assign_stmt).scalar_one() or 0)

    bind_stmt = (
        select(func.count(DeviceBinding.id))
        .join(Device, Device.id == DeviceBinding.device_id)
        .where(DeviceBinding.status == "active")
    )
    bind_stmt = _tenant_filter(bind_stmt, tenant_scope, Device.tenant_id)
    bound_devices = int(db.execute(bind_stmt).scalar_one() or 0)

    # --- 在库可领用：未领用 且 生命周期允许分配 ---
    idle_stmt = (
        select(func.count(Device.id))
        .outerjoin(
            DeviceAssignment,
            (DeviceAssignment.device_id == Device.id)
            & (DeviceAssignment.returned_at.is_(None)),
        )
        .where(DeviceAssignment.id.is_(None))
        .where(Device.lifecycle_status.in_([s.value for s in ASSIGNABLE_LIFECYCLE_STATUSES]))
    )
    idle_stmt = _tenant_filter(idle_stmt, tenant_scope, Device.tenant_id)
    idle_devices = int(db.execute(idle_stmt).scalar_one() or 0)

    # --- 领用中的去重员工数 ---
    employee_stmt = select(func.count(func.distinct(DeviceAssignment.user_id))).join(
        Device, Device.id == DeviceAssignment.device_id
    ).where(DeviceAssignment.returned_at.is_(None))
    employee_stmt = _tenant_filter(employee_stmt, tenant_scope, Device.tenant_id)
    employee_count = int(db.execute(employee_stmt).scalar_one() or 0)

    # --- 设备状态聚合 ---
    status_stmt = select(DeviceStatus).join(Device, Device.id == DeviceStatus.device_id)
    status_stmt = _tenant_filter(status_stmt, tenant_scope, Device.tenant_id)
    statuses = db.execute(status_stmt).scalars().all()

    online_devices = sum(1 for s in statuses if s.online)
    charging_devices = sum(1 for s in statuses if s.charging)
    recording_devices = sum(1 for s in statuses if s.recording)
    battery_values = [s.battery_level for s in statuses if s.battery_level is not None]
    avg_battery = round(sum(battery_values) / len(battery_values), 1) if battery_values else None

    # --- 凭证与绑定总量 ---
    cred_stmt = select(DeviceCredential.status, func.count(DeviceCredential.id)).join(
        Device, Device.id == DeviceCredential.device_id
    )
    cred_stmt = _tenant_filter(cred_stmt, tenant_scope, Device.tenant_id)
    active_credentials = 0
    total_credentials = 0
    for status_value, count in db.execute(cred_stmt).all():
        total_credentials += int(count)
        if str(status_value) == "active":
            active_credentials = int(count)

    binding_total_stmt = select(func.count(DeviceBinding.id)).join(
        Device, Device.id == DeviceBinding.device_id
    )
    binding_total_stmt = _tenant_filter(binding_total_stmt, tenant_scope, Device.tenant_id)
    total_bindings = int(db.execute(binding_total_stmt).scalar_one() or 0)

    assignment_total_stmt = select(func.count(DeviceAssignment.id)).join(
        Device, Device.id == DeviceAssignment.device_id
    )
    assignment_total_stmt = _tenant_filter(assignment_total_stmt, tenant_scope, Device.tenant_id)
    total_assignments = int(db.execute(assignment_total_stmt).scalar_one() or 0)

    # --- 事件统计 ---
    event_scope = select(DeviceEvent.id).join(Device, Device.id == DeviceEvent.device_id)
    event_scope = _tenant_filter(event_scope, tenant_scope, Device.tenant_id)

    events_today = int(
        db.execute(
            select(func.count()).select_from(event_scope.where(DeviceEvent.created_at >= today).subquery())
        ).scalar_one()
        or 0
    )
    events_7d = int(
        db.execute(
            select(func.count()).select_from(event_scope.where(DeviceEvent.created_at >= week_ago).subquery())
        ).scalar_one()
        or 0
    )

    type_stmt = (
        select(DeviceEvent.event_type, func.count(DeviceEvent.id))
        .join(Device, Device.id == DeviceEvent.device_id)
        .group_by(DeviceEvent.event_type)
    )
    type_stmt = _tenant_filter(type_stmt, tenant_scope, Device.tenant_id)
    event_types: Dict[str, int] = {
        str(event_type): int(count) for event_type, count in db.execute(type_stmt).all()
    }

    # --- 领用设备数 TOP ---
    top_stmt = (
        select(DeviceAssignment.user_id, func.count(DeviceAssignment.id).label("device_count"))
        .join(Device, Device.id == DeviceAssignment.device_id)
        .where(DeviceAssignment.returned_at.is_(None))
        .group_by(DeviceAssignment.user_id)
        .order_by(func.count(DeviceAssignment.id).desc())
        .limit(10)
    )
    top_stmt = _tenant_filter(top_stmt, tenant_scope, Device.tenant_id)
    top_assignees = [
        TopAssignee(user_id=str(user_id), device_count=int(count))
        for user_id, count in db.execute(top_stmt).all()
    ]

    return OverviewStats(
        total_devices=total_devices,
        lifecycle=lifecycle,
        assigned_devices=assigned_devices,
        bound_devices=bound_devices,
        idle_devices=idle_devices,
        unassigned_employee_count=employee_count,
        online_devices=online_devices,
        offline_devices=len(statuses) - online_devices,
        charging_devices=charging_devices,
        recording_devices=recording_devices,
        avg_battery=avg_battery,
        active_credentials=active_credentials,
        total_credentials=total_credentials,
        active_bindings=bound_devices,
        total_bindings=total_bindings,
        total_assignments=total_assignments,
        events_today=events_today,
        events_7d=events_7d,
        top_assignees=top_assignees,
        event_types=event_types,
    )


__all__ = [
    "list_assignments",
    "list_bindings",
    "list_credentials",
    "list_statuses",
    "list_events",
    "overview_stats",
]
