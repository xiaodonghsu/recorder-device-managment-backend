"""统一异常体系。

所有业务异常继承自 AppError，由全局异常处理器转换为标准 JSON 响应，
HTTP 状态码与错误码一一对应，便于 APP / 管理后台做精细化处理。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """业务异常基类。"""

    status_code: int = 400
    error_code: str = "bad_request"
    message: str = "请求无法处理"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        error_code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.message
        self.error_code = error_code or self.error_code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.error_code,
            "message": self.message,
            "status": self.status_code,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "资源不存在"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    message = "资源状态冲突"


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"
    message = "参数校验失败"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "unauthorized"
    message = "身份认证失败"


class PermissionDeniedError(AppError):
    status_code = 403
    error_code = "forbidden"
    message = "无权访问该资源"


class DeviceStateError(AppError):
    """设备生命周期状态不允许当前操作。"""

    status_code = 409
    error_code = "invalid_device_state"
    message = "设备当前状态不允许该操作"


class CasdoorError(AppError):
    """Casdoor 交互异常。"""

    status_code = 502
    error_code = "casdoor_error"
    message = "身份认证服务异常"
