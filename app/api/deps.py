"""FastAPI 依赖：Casdoor 认证、身份上下文与权限判定。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import (
    AuthenticationError,
    CasdoorError,
    PermissionDeniedError,
    ValidationError,
)
from app.integrations.casdoor import CasdoorClient, CasdoorIdentity, get_casdoor_client

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="Casdoor Access Token")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class CurrentUser:
    """请求级身份上下文。

    tenant_scope 的语义：
    - ``None``：跨组织管理员，可访问全部租户；
    - 列表：仅能访问列表内的租户（普通用户固定为自身所属企业）。
    """

    identity: CasdoorIdentity
    # 原始 access token 仅在「以当前管理员身份代查 Casdoor 用户」时使用。
    # 严禁写进日志、事件数据或任何响应体。
    access_token: Optional[str] = None

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    @property
    def username(self) -> str:
        return self.identity.username

    @property
    def display_name(self) -> str:
        return self.identity.display_name

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    @property
    def employee_no(self) -> str:
        """业务侧员工唯一标识：工号（Casdoor ``preferred_username``）。

        写入 ``device_assignment.user_id`` / ``device_binding.user_id`` 的值，
        与 ``user_id``（Token 里的技术 UUID）不是同一个东西。
        """
        return self.identity.employee_no

    @property
    def user_refs(self) -> List[str]:
        """查询业务表 ``user_id`` 列时可接受的标识集合。

        规范值是工号；额外纳入技术 UUID，只为兼容按 UUID 写入的历史领用记录。
        **不包含** ``display_name``（姓名可能重名，不能作为唯一标识）。
        """
        refs = {
            str(value).strip()
            for value in (self.identity.employee_no, self.identity.user_id)
            if value and str(value).strip()
        }
        return sorted(refs)

    @property
    def is_admin(self) -> bool:
        return self.identity.is_admin

    @property
    def is_global_admin(self) -> bool:
        return self.identity.is_global_admin

    @property
    def tenant_scope(self) -> Optional[List[str]]:
        if self.is_global_admin:
            return None
        return [self.tenant_id]


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    """校验 Casdoor Access Token 并返回身份上下文。"""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("缺少访问令牌，请先通过 Casdoor 登录")

    client: CasdoorClient = get_casdoor_client(request)
    identity = await client.verify_token(credentials.credentials)
    return CurrentUser(identity=identity, access_token=credentials.credentials)


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """要求管理员角色（README §6：管理员接口需要额外检查企业和角色权限）。

    403 时返回当前用户的角色 / 用户组 / 权限，便于自助定位判定依据。
    """
    if not user.is_admin:
        identity = user.identity
        detail = (
            f"当前用户 {identity.username}（角色={identity.roles or '无'}，"
            f"用户组={identity.groups or '无'}，权限={identity.permissions or '无'}）"
            "未命中管理员判定条件。"
        )
        raise PermissionDeniedError("该操作需要管理员权限", details={"reason": detail})
    return user


async def require_employee(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """任意已认证员工。"""
    return user


def resolve_target_tenant(user: CurrentUser, requested: Optional[str]) -> str:
    """确定目标企业。

    客户端提交的 tenant_id 只作为「请求」，最终以服务端判定为准：
    普通用户与组织管理员只能操作自身企业；跨组织管理员可显式指定其它企业。
    """
    scope = user.tenant_scope
    if scope is None:
        return requested or user.tenant_id
    if requested and requested not in scope:
        raise PermissionDeniedError("无权操作其它企业的设备")
    return user.tenant_id


async def resolve_employee_id(
    client: CasdoorClient,
    tenant_id: str,
    user_ref: str,
    *,
    access_token: Optional[str] = None,
) -> str:
    """把领用表单输入规范化为**工号**，并校验该工号在 Casdoor 中真实存在。

    业务约定：``device_assignment.user_id`` 存工号（``preferred_username``），
    不是 Casdoor UUID、也不是姓名。校验规则：

    - 先按 ``CASDOOR_VERIFY_EMPLOYEE`` 决定强度（off / optional / required）；
    - 工号命中 → 写回 Casdoor 的规范 ``name``（查询大小写不敏感，管理员输入
      ``jh000059`` 也会规范化成 ``JH000059``）；
    - 明确查无此人 / 组织不符 / 账号禁用 → 拒绝分配，不产生孤儿领用记录；
    - Casdoor 查询本身不可用 → optional 降级放行并告警，required 直接 502；
    - UUID 输入不是工号：尝试反查，反查不到则明确报错，绝不静默写入业务表。

    本实例的 ``/api/get-user`` 是公开只读接口，因此**不依赖 CASDOOR_CLIENT_SECRET**
    也能完成校验；若 Casdoor 收紧权限，可配服务令牌或透传管理员 access token。
    """
    value = (user_ref or "").strip()
    if not value:
        raise ValidationError("员工工号不能为空")

    mode = (getattr(client.cfg, "casdoor_verify_employee", "optional") or "optional").lower()

    if _UUID_RE.match(value):
        user_obj = None
        if mode != "off":
            user_obj = await client.get_user(tenant_id, value, access_token=access_token)
        resolved = None
        if isinstance(user_obj, dict):
            resolved = user_obj.get("name") or user_obj.get("preferred_username")
        if not resolved:
            raise ValidationError(
                "填入的是 Casdoor 用户 UUID，不是工号；请填 preferred_username（如 JH000059）",
                error_code="employee_no_required",
                details={"input": value, "tenant_id": tenant_id},
            )
        value = str(resolved)

    if mode == "off":
        return value

    result = await client.lookup_employee(tenant_id, value, access_token=access_token)
    if result.found:
        logger.info(
            "工号校验通过：%s（%s，tenant=%s）",
            result.employee_no,
            result.display_name or "-",
            tenant_id,
        )
        return result.employee_no or value

    if result.status == "absent":
        raise ValidationError(
            f"员工工号 {value!r} 校验未通过：{result.reason}",
            error_code="employee_not_found",
            details={
                "employee_no": value,
                "tenant_id": tenant_id,
                "reason": result.reason,
            },
        )

    reason = result.reason or "Casdoor 用户查询不可用"
    if mode == "required":
        raise CasdoorError(
            f"无法校验员工工号 {value!r}：{reason}",
            details={"employee_no": value, "tenant_id": tenant_id},
        )
    logger.warning(
        "工号 %r 未能完成 Casdoor 存在性校验（%s），按 "
        "CASDOOR_VERIFY_EMPLOYEE=optional 放行；要求强一致请设为 required。",
        value,
        reason,
    )
    return value
