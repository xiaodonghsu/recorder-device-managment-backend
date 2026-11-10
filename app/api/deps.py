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
    def user_refs(self) -> List[str]:
        """可与业务表 ``user_id`` 列比对的全部身份标识。

        Token 里的 ``user_id`` 是 Casdoor 用户 UUID，但领用记录可能写入了
        用户名 / 工号：``resolve_employee_id`` 在未配置 CASDOOR_CLIENT_SECRET、
        无法调用 Casdoor 反查时会按原值透传。若只做 UUID 精确匹配，就会出现
        「后台分配成功，员工登录后 /my/devices 却是空列表」。

        Casdoor 的 ``name`` 在同一组织内唯一，配合 tenant 过滤不会放宽越权范围。
        """
        refs = {
            str(value).strip()
            for value in (self.identity.user_id, self.identity.username)
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
    """把「用户名 / 工号 / 用户 id」解析为 Casdoor 用户 id。

    - 形如 UUID 的直接作为用户 id；
    - 否则向 Casdoor 查询该组织下的用户（支持用户名与 ``owner/name`` 两种形式）；
    - 查询不到时按原值透传（兼容外部用户中心自定义标识），但会记录告警，
      因为该值可能与员工 Token 中的 UUID 不一致，导致「我的设备」查不到。
    """
    value = (user_ref or "").strip()
    if not value:
        raise ValidationError("员工标识不能为空")
    if _UUID_RE.match(value):
        return value

    user_obj = await client.get_user(tenant_id, value)
    if user_obj and isinstance(user_obj, dict):
        resolved = user_obj.get("id")
        if resolved:
            return str(resolved)

    logger.warning(
        "员工标识 %r 未能反查为 Casdoor 用户 UUID（tenant=%s，客户端凭据=%s），"
        "将按原值写入领用记录。请配置 CASDOOR_CLIENT_SECRET 或改填用户 UUID，"
        "否则该员工登录后 /my/devices 可能因标识不匹配而返回空列表。",
        value,
        tenant_id,
        "已配置" if client.cfg.casdoor_has_client_credentials else "缺失",
    )
    return value
