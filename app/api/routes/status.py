"""手机端设备状态上报接口（README §5.4 / APP 对接说明 §6.3）。

录音卡固件不与后端通信，本端点是 ``device_status`` 唯一的写入路径：
由领用该设备的员工用 Casdoor Token 代报蓝牙实测状态，服务端校验归属、
裁决 online / updated_at，并覆盖单行状态快照。管理后台仍走只读接口
``GET /devices/{id}/status`` 与 ``GET /tables/statuses``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.device import DeviceStatusAck, DeviceStatusReport
from app.services import status_service

router = APIRouter(tags=["设备状态上报"])


@router.put(
    "/my/devices/{device_id}/status",
    response_model=DeviceStatusAck,
    summary="上报设备状态（APP 代报）",
    description=(
        "员工上报自己领用且已绑定设备的蓝牙实测状态（电量、充电、录音、连接）。"
        "服务端校验当前领用关系、有效绑定与租户范围；online 与 updated_at "
        "由服务端裁决，客户端传入 online / updated_at 会被拒绝。"
        "建议事件驱动 + 30~60 秒心跳，响应中的 heartbeat_interval_seconds "
        "为建议间隔，server_time 供 APP 校时。"
    ),
)
async def report_device_status(
    payload: DeviceStatusReport,
    device_id: int = Path(ge=1),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> DeviceStatusAck:
    return status_service.report_status(
        db,
        device_id,
        payload,
        user_refs=user.user_refs,
        tenant_scope=user.tenant_scope,
        operator_id=user.user_id,
        reporter=user.employee_no,
    )
