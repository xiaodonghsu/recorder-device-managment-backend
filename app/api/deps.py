"""FastAPI 依赖：Casdoor 认证、身份上下文与权限判定。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AuthenticationError, PermissionDeniedError, ValidationError
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
    return CurrentUser(identity=identity)


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
) -> str:
    """把领用表单里的输入规范化为**工号**（``preferred_username``）。

    业务约定：``device_assignment.user_id`` 存工号，不是 Casdoor UUID。

    - 非 UUID 输入：视为工号，直接返回。**不依赖 CASDOOR_CLIENT_SECRET**，
      也不需要访问 Casdoor，管理员按工号分配即可直接生效；
    - UUID 输入：尝试用服务令牌反查对应的工号；反查失败时告警并原值返回，
      以兼容按 UUID 写入的历史数据。
    """
    value = (user_ref or "").strip()
    if not value:
        raise ValidationError("员工工号不能为空")
    if not _UUID_RE.match(value):
        return value

    user_obj = await client.get_user(tenant_id, value)
    if user_obj and isinstance(user_obj, dict):
        resolved = user_obj.get("preferred_username") or user_obj.get("name")
        if resolved:
            return str(resolved)

    logger.warning(
        "输入 %r 是用户 UUID，未能反查为工号（tenant=%s，客户端凭据=%s），"
        "将按原值写入领用记录；建议改填工号 preferred_username。",
        value,
        tenant_id,
        "已配置" if client.cfg.casdoor_has_client_credentials else "缺失",
    )
    return value
