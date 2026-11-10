"""设备领用 / 归还相关契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AssignmentCreate(BaseModel):
    """设备领用请求（README §5.2，管理员操作）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: str = Field(
        min_length=1,
        max_length=64,
        description="领用员工工号（Casdoor preferred_username，如 JH000059）",
    )
    tenant_id: Optional[str] = Field(
        default=None, max_length=64, description="目标企业；不填则归属自身企业"
    )
    note: Optional[str] = Field(default=None, max_length=255, description="领用备注")


class AssignmentOut(BaseModel):
    """领用记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    tenant_id: str
    user_id: str
    assigned_by: Optional[str] = None
    assigned_at: datetime
    returned_at: Optional[datetime] = None
    return_reason: Optional[str] = None
    created_at: datetime

    device_sn: Optional[str] = None
    user_display_name: Optional[str] = None


class DeviceReturnRequest(BaseModel):
    """设备归还请求（README §5.4，管理员操作）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: Optional[str] = Field(default=None, max_length=255, description="归还原因")
    revoke_binding: bool = Field(
        default=True, description="归还时是否同时解除员工的 APP 绑定（默认解除）"
    )
