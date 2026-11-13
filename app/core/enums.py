"""业务枚举与状态机常量（README §3.1 / §3.6）。"""

from __future__ import annotations

from enum import Enum
from typing import Set


class LifecycleStatus(str, Enum):
    """设备生命周期状态。"""

    REGISTERED = "registered"
    ACTIVE = "active"
    DISABLED = "disabled"
    LOST = "lost"
    REPAIR = "repair"
    RETIRED = "retired"


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class BindingStatus(str, Enum):
    ACTIVE = "active"
    UNBOUND = "unbound"
    REVOKED = "revoked"


class EventType(str, Enum):
    """设备生命周期事件类型。"""

    DEVICE_REGISTERED = "device_registered"
    DEVICE_ACTIVATED = "device_activated"
    DEVICE_ASSIGNED = "device_assigned"
    DEVICE_RETURNED = "device_returned"
    DEVICE_BOUND = "device_bound"
    DEVICE_UNBOUND = "device_unbound"
    CREDENTIAL_ROTATED = "credential_rotated"
    DEVICE_DISABLED = "device_disabled"
    DEVICE_LOST = "device_lost"
    DEVICE_REPAIRED = "device_repaired"
    DEVICE_RETIRED = "device_retired"
    # 扩展事件（README §6 未列举，但对运维追溯有价值）
    DEVICE_ENABLED = "device_enabled"
    CREDENTIAL_REVOKED = "credential_revoked"
    NICKNAME_UPDATED = "nickname_updated"
    DEVICE_PROFILE_UPDATED = "device_profile_updated"
    BINDING_REVOKED = "binding_revoked"
    # APP 状态上报：仅在在线/充电/录音发生跃迁时记录，高频心跳不逐条留痕
    DEVICE_STATUS_CHANGED = "device_status_changed"


# 允许员工发起绑定的设备状态（README §7.8：禁用、挂失、报废设备不得创建新绑定）
BINDABLE_LIFECYCLE_STATUSES: Set[LifecycleStatus] = {
    LifecycleStatus.REGISTERED,
    LifecycleStatus.ACTIVE,
}

# 允许分配给员工的设备状态
ASSIGNABLE_LIFECYCLE_STATUSES: Set[LifecycleStatus] = {
    LifecycleStatus.REGISTERED,
    LifecycleStatus.ACTIVE,
}

# 终态，不可再流转
TERMINAL_LIFECYCLE_STATUSES: Set[LifecycleStatus] = {
    LifecycleStatus.RETIRED,
}

# 允许流转的目标状态
LIFECYCLE_TRANSITIONS: dict[LifecycleStatus, Set[LifecycleStatus]] = {
    LifecycleStatus.REGISTERED: {
        LifecycleStatus.ACTIVE,
        LifecycleStatus.DISABLED,
        LifecycleStatus.LOST,
        LifecycleStatus.REPAIR,
        LifecycleStatus.RETIRED,
    },
    LifecycleStatus.ACTIVE: {
        LifecycleStatus.DISABLED,
        LifecycleStatus.LOST,
        LifecycleStatus.REPAIR,
        LifecycleStatus.RETIRED,
    },
    LifecycleStatus.DISABLED: {
        LifecycleStatus.ACTIVE,
        LifecycleStatus.LOST,
        LifecycleStatus.REPAIR,
        LifecycleStatus.RETIRED,
    },
    LifecycleStatus.LOST: {
        LifecycleStatus.ACTIVE,
        LifecycleStatus.REPAIR,
        LifecycleStatus.RETIRED,
    },
    LifecycleStatus.REPAIR: {
        LifecycleStatus.ACTIVE,
        LifecycleStatus.DISABLED,
        LifecycleStatus.LOST,
        LifecycleStatus.RETIRED,
    },
    LifecycleStatus.RETIRED: set(),
}

# 允许修改设备档案字段的生命周期状态（报废后锁定档案）
PROFILE_EDITABLE_STATUSES: Set[LifecycleStatus] = {
    LifecycleStatus.REGISTERED,
    LifecycleStatus.ACTIVE,
    LifecycleStatus.DISABLED,
    LifecycleStatus.LOST,
    LifecycleStatus.REPAIR,
}
