"""APP 绑定 / 解绑 / 昵称相关契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BindingCreate(BaseModel):
    """APP 绑定请求（README §5.3，员工操作）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    sn: Optional[str] = Field(
        default=None, max_length=64, description="设备 SN；用于二次确认绑定目标"
    )
    nickname: Optional[str] = Field(
        default=None, max_length=64, description="员工自定义昵称，可稍后单独修改"
    )

    @field_validator("nickname")
    @classmethod
    def _check_nickname(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        if not value:
            return None
        return value


class BindingOut(BaseModel):
    """绑定记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    assignment_id: int
    user_id: str
    nickname: Optional[str] = None
    status: str
    bound_at: datetime
    unbound_at: Optional[datetime] = None
    created_at: datetime

    device_sn: Optional[str] = None
    device_model: Optional[str] = None


class NicknameUpdate(BaseModel):
    """昵称修改请求（README §6 PUT /devices/{id}/nickname）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    nickname: str = Field(min_length=1, max_length=64, description="员工自定义昵称")

    @field_validator("nickname")
    @classmethod
    def _check_nickname(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("昵称不能为空")
        return value
