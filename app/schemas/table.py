"""表格级（跨设备）查询契约。

README §3 的 6 张表都需要在管理后台中「分别管理」，而设备子资源接口
（/api/devices/{id}/...）只能按单台设备过滤。本模块定义各表的行结构与
仪表盘聚合结果，供 /api/tables/* 使用。

所有结构均为**只读投影**，不提供写入能力：写操作仍走设备子资源接口，
以复用服务层的状态机、事务与事件记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AssignmentRow(BaseModel):
    """device_assignment 行（领用记录）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_sn: Optional[str] = None
    device_model: Optional[str] = None
    tenant_id: str
    user_id: str
    assigned_by: Optional[str] = None
    assigned_at: datetime
    returned_at: Optional[datetime] = None
    return_reason: Optional[str] = None
    created_at: datetime
    is_current: bool = Field(default=True, description="是否仍在领用中")


class BindingRow(BaseModel):
    """device_binding 行（APP 绑定记录）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_sn: Optional[str] = None
    device_model: Optional[str] = None
    assignment_id: int
    user_id: str
    nickname: Optional[str] = None
    status: str
    bound_at: datetime
    unbound_at: Optional[datetime] = None
    created_at: datetime


class CredentialRow(BaseModel):
    """device_credential 行（设备凭证，不含 AuthKey 明文）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_sn: Optional[str] = None
    auth_key_version: int
    status: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class StatusRow(BaseModel):
    """device_status 行（设备最新状态）。"""

    model_config = ConfigDict(from_attributes=True)

    device_id: int
    device_sn: Optional[str] = None
    device_model: Optional[str] = None
    tenant_id: Optional[str] = None
    lifecycle_status: Optional[str] = None
    online: bool = False
    battery_level: Optional[int] = None
    charging: bool = False
    recording: bool = False
    last_seen_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EventRow(BaseModel):
    """device_event 行（生命周期事件）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_sn: Optional[str] = None
    event_type: str
    operator_id: Optional[str] = None
    event_data: Optional[dict] = None
    created_at: datetime


class LifecycleBreakdown(BaseModel):
    """按生命周期状态分布。"""

    registered: int = 0
    active: int = 0
    disabled: int = 0
    lost: int = 0
    repair: int = 0
    retired: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "registered": self.registered,
            "active": self.active,
            "disabled": self.disabled,
            "lost": self.lost,
            "repair": self.repair,
            "retired": self.retired,
        }


class TopAssignee(BaseModel):
    """领用设备数排行条目。"""

    user_id: str
    device_count: int


class EmployeeRow(BaseModel):
    """员工条目（由领用 / 绑定记录反推）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    device_count: int = 0
    binding_count: int = 0
    last_assigned_at: Optional[datetime] = None


class OverviewStats(BaseModel):
    """管理后台概览聚合指标。"""

    total_devices: int = 0
    lifecycle: LifecycleBreakdown = Field(default_factory=LifecycleBreakdown)

    assigned_devices: int = Field(default=0, description="当前领用中的设备数")
    bound_devices: int = Field(default=0, description="存在有效 APP 绑定的设备数")
    idle_devices: int = Field(default=0, description="在库可领用（未领用且状态可分配）")
    unassigned_employee_count: int = Field(default=0, description="当前领用设备涉及的去重员工数")

    online_devices: int = 0
    offline_devices: int = 0
    charging_devices: int = 0
    recording_devices: int = 0
    avg_battery: Optional[float] = None

    active_credentials: int = 0
    total_credentials: int = 0
    active_bindings: int = 0
    total_bindings: int = 0
    total_assignments: int = 0

    events_today: int = 0
    events_7d: int = 0
    top_assignees: List[TopAssignee] = Field(default_factory=list)
    event_types: Dict[str, int] = Field(default_factory=dict)


__all__ = [
    "AssignmentRow",
    "BindingRow",
    "CredentialRow",
    "StatusRow",
    "EventRow",
    "LifecycleBreakdown",
    "TopAssignee",
    "EmployeeRow",
    "OverviewStats",
]
