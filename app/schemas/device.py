"""设备档案相关的请求/响应契约。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import LifecycleStatus
from app.core.security import is_auth_key_plausible


class DeviceCreate(BaseModel):
    """设备入库请求（README §5.1）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    sn: str = Field(min_length=1, max_length=64, description="设备 SN，全局唯一，不允许员工修改")
    bluetooth_name: Optional[str] = Field(default=None, max_length=128, description="蓝牙广播名称，可重复")
    model: Optional[str] = Field(default=None, max_length=64, description="设备型号")
    manufacturer: Optional[str] = Field(default=None, max_length=100, description="厂商")
    hardware_version: Optional[str] = Field(default=None, max_length=32)
    firmware_version: Optional[str] = Field(default=None, max_length=32)
    production_batch: Optional[str] = Field(default=None, max_length=64)
    auth_key: Optional[str] = Field(
        default=None,
        max_length=128,
        description="导入已有 AuthKey；不传则由系统生成高强度随机值",
    )
    tenant_id: Optional[str] = Field(
        default=None, max_length=64, description="目标企业；管理员不填则归属自身企业"
    )

    @field_validator("sn")
    @classmethod
    def _check_sn(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("SN 不能为空")
        if any(ch.isspace() for ch in value):
            raise ValueError("SN 不能包含空白字符")
        return value

    @field_validator("auth_key")
    @classmethod
    def _check_auth_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        if not value:
            return None
        if not is_auth_key_plausible(value):
            raise ValueError("AuthKey 格式非法（长度需为 16-128，且仅含字母数字与 _-）")
        return value


class DeviceBatchCreate(BaseModel):
    """设备批量入库请求（README §8 P0）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    items: List[DeviceCreate] = Field(min_length=1, max_length=1000, description="待入库设备清单")
    continue_on_error: bool = Field(default=True, description="单条失败是否继续处理其余设备")

    @field_validator("items")
    @classmethod
    def _check_unique_sn(cls, v: List[DeviceCreate]) -> List[DeviceCreate]:
        sns = [item.sn for item in v]
        duplicates = {sn for sn in sns if sns.count(sn) > 1}
        if duplicates:
            raise ValueError(f"批量清单中存在重复 SN: {', '.join(sorted(duplicates))}")
        return v


class DeviceBatchResultItem(BaseModel):
    """批量入库的单条结果。"""

    sn: str
    success: bool
    device_id: Optional[int] = None
    auth_key: Optional[str] = Field(default=None, description="系统生成的 AuthKey 明文")
    error: Optional[str] = None


class DeviceBatchResult(BaseModel):
    """批量入库汇总。"""

    total: int
    succeeded: int
    failed: int
    items: List[DeviceBatchResultItem]


class DeviceUpdate(BaseModel):
    """设备档案更新（SN 不可修改）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    bluetooth_name: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=64)
    manufacturer: Optional[str] = Field(default=None, max_length=100)
    hardware_version: Optional[str] = Field(default=None, max_length=32)
    firmware_version: Optional[str] = Field(default=None, max_length=32)
    production_batch: Optional[str] = Field(default=None, max_length=64)


class DeviceStatusOut(BaseModel):
    """设备最新状态（device_status 表）。"""

    model_config = ConfigDict(from_attributes=True)

    device_id: int
    online: bool = False
    battery_level: Optional[int] = None
    charging: bool = False
    recording: bool = False
    last_seen_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CredentialOut(BaseModel):
    """设备凭证信息（不含 AuthKey 明文）。"""

    model_config = ConfigDict(from_attributes=True)

    device_id: int
    auth_key_version: int
    status: str
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class CredentialIssuedOut(BaseModel):
    """凭证签发结果，包含新 AuthKey 明文。"""

    device_id: int
    auth_key: str = Field(description="新签发的 AuthKey 明文")
    auth_key_version: int
    issued_at: datetime
    expires_at: Optional[datetime] = None


class DeviceOut(BaseModel):
    """设备详情。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    sn: str
    bluetooth_name: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    hardware_version: Optional[str] = None
    firmware_version: Optional[str] = None
    production_batch: Optional[str] = None
    lifecycle_status: LifecycleStatus
    activated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # 关联信息
    assigned: bool = Field(default=False, description="当前是否处于领用中")
    assigned_user_id: Optional[str] = None
    assigned_at: Optional[datetime] = None
    bound: bool = Field(default=False, description="当前是否存在有效 APP 绑定")
    bound_user_id: Optional[str] = None
    nickname: Optional[str] = None
    status: Optional[DeviceStatusOut] = None


class MyDeviceOut(DeviceOut):
    """员工领用列表中的设备信息。"""

    auth_key: Optional[str] = Field(
        description="当前有效的 AuthKey 明文，缺少可用凭证时为 null"
    )


class DeviceListItem(DeviceOut):
    """设备列表项（在详情基础上补充当前绑定信息）。"""

    binding_status: Optional[str] = None


class DeviceCreatedOut(DeviceOut):
    """设备入库结果，包含系统生成的 AuthKey 明文。"""

    auth_key: Optional[str] = Field(default=None, description="系统生成的 AuthKey 明文")


class DeviceEventOut(BaseModel):
    """设备事件（device_event 表）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    event_type: str
    operator_id: Optional[str] = None
    event_data: Optional[dict] = None
    created_at: datetime
