"""设备事件记录服务（README §3.6 / §7.10）。

所有管理操作都必须落一条 device_event。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.enums import EventType
from app.db.models import DeviceEvent


def record_event(
    db: Session,
    device_id: int,
    event_type: EventType | str,
    *,
    operator_id: Optional[str] = None,
    event_data: Optional[Dict[str, Any]] = None,
) -> DeviceEvent:
    """写入一条设备事件。"""
    event = DeviceEvent(
        device_id=device_id,
        event_type=event_type.value if isinstance(event_type, EventType) else str(event_type),
        operator_id=operator_id,
        event_data=event_data or {},
    )
    db.add(event)
    db.flush()
    return event
