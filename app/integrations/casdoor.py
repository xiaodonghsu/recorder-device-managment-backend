"""Casdoor 身份认证集成。

Casdoor 实例地址形如 ``https://host:port/auth``，注意其 ``authPrefix=auth``，
因此所有接口都在 ``/auth`` 之下：

===========================================  ==========================================
接口                                          用途
===========================================  ==========================================
``{base}/api/userinfo``                       用 Bearer Token 换取完整用户对象
``{base}/api/login/oauth/introspect``        服务端校验 Token 有效性（OAuth2 RFC7662）
``{base}/api/login/oauth/access_token``      client_credentials 换取服务令牌
``{base}/api/get-user``                      按 owner + id/name 查询用户
``{base}/.well-known/openid-configuration``  OIDC 发现文档
``{base}/.well-known/jwks``                  JWT 验签公钥
===========================================  ==========================================

本模块把上述差异收敛为 ``CasdoorClient.verify_token()``，并输出与业务无关的
``CasdoorIdentity``。业务层只依赖 tenant_id / user_id / is_admin 三个概念，
不直接感知 Casdoor 的字段细节。

Casdoor 用户对象中的租户语义：

- ``owner``：用户所属组织（Organization），本服务映射为 ``tenant_id``；
- ``id``：用户全局唯一标识，映射为 ``user_id``；
- ``role`` / ``roles``：用户在组织内的角色，映射为管理员判定依据；
- ``groups``：用户所属用户组，映射为管理员判定依据；
- ``isAdmin``：Casdoor 内置的管理员标记。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx
from fastapi import Request
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

from app.core.config import Settings
from app.core.config import settings as global_settings
from app.core.errors import AuthenticationError, CasdoorError

logger = logging.getLogger(__name__)


@dataclass
class CasdoorIdentity:
    """与业务无关的已认证身份。"""

    user_id: str
    username: str
    display_name: str
    tenant_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    is_admin: bool = False
    is_global_admin: bool = False
    properties: Dict[str, Any] = field(default_factory=dict)
    auth_method: str = "unknown"
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.username


def _first(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return default


class CasdoorClient:
    """Casdoor REST/OIDC 客户端。"""

    def __init__(self, cfg: Settings, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = cfg
        self._http = http_client
        self._owns_http = http_client is None
        self._jwks_cache: Dict[str, Any] = {}
        self._jwks_fetched_at: float = 0.0
        self._service_token: Optional[str] = None
        self._service_token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self.cfg.casdoor_base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.cfg.casdoor_timeout,
                verify=self.cfg.casdoor_verify_tls,
                follow_redirects=True,
            )
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # Token 校验
    # ------------------------------------------------------------------
    async def verify_token(self, token: str) -> CasdoorIdentity:
        """校验 access token 并返回身份。"""
        if not token or not token.strip():
            raise AuthenticationError("缺少访问令牌")

        method = self.cfg.casdoor_verify_method
        if method == "jwt":
            return await self._verify_by_jwt(token)
        if method == "introspect":
            return await self._verify_by_introspect(token)
        if method == "userinfo":
            return await self._verify_by_userinfo(token)

        # auto：优先 userinfo（能拿到完整用户对象），失败再退回 introspect
        try:
            return await self._verify_by_userinfo(token)
        except AuthenticationError as exc:
            if not self.cfg.casdoor_has_client_credentials:
                # 没有客户端凭据时 introspect 不可用，直接返回原始错误
                raise
            logger.info("userinfo 校验失败（%s），回退到 introspect 模式", exc.message)
        return await self._verify_by_introspect(token)

    async def _verify_by_userinfo(self, token: str) -> CasdoorIdentity:
        data = await self._get_json("/api/userinfo", headers={"Authorization": f"Bearer {token}"})
        if not data:
            raise AuthenticationError("用户信息获取失败")
        if isinstance(data.get("data"), dict) and not data.get("id"):
            data = data["data"]
        return self._identity_from_user_object(data, auth_method="userinfo")

    async def _verify_by_introspect(self, token: str) -> CasdoorIdentity:
        if not self.cfg.casdoor_has_client_credentials:
            raise AuthenticationError("introspect 模式需要配置 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET")

        form = {"token": token, "token_type_hint": "access_token"}
        response = await self._post_form("/api/login/oauth/introspect", form=form, basic_auth=True)

        if not response.get("active"):
            raise AuthenticationError("访问令牌无效或已过期")

        sub = _first(response, "sub", "user_id", "uid")
        username = _first(response, "username", "name", "preferred_username")
        if not sub and not username:
            raise AuthenticationError("访问令牌缺少用户标识")

        # introspect 返回的信息有限，尝试用服务令牌补全用户对象
        user_obj: Dict[str, Any] = {}
        try:
            service_token = await self.get_service_token()
            if service_token:
                user_obj = await self._get_json(
                    "/api/get-account",
                    params={"owner": self.cfg.casdoor_organization},
                    headers={"Authorization": f"Bearer {service_token}"},
                )
                if not isinstance(user_obj.get("id"), str):
                    user_obj = {}
        except Exception as exc:  # noqa: BLE001 - 补全失败不应阻断主流程
            logger.warning("补全用户信息失败：%s", exc)

        if user_obj:
            identity = self._identity_from_user_object(user_obj, auth_method="introspect")
            identity.raw = {**response, **{"introspect": True}}
            return identity

        tenant = self.cfg.casdoor_organization
        identity = CasdoorIdentity(
            user_id=str(sub or username),
            username=str(username or sub or ""),
            display_name=str(username or sub or ""),
            tenant_id=tenant,
            auth_method="introspect",
            raw=response,
        )
        self._apply_admin_flags(identity)
        return identity

    async def _verify_by_jwt(self, token: str) -> CasdoorIdentity:
        try:
            header = jose_jwt.get_unverified_header(token)
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError("令牌格式非法") from exc

        kid = header.get("kid")
        await self._get_jwks()  # 确保公钥缓存已填充（首次会发起网络请求）
        key_data = self._find_jwk(kid)
        if key_data is None:
            raise AuthenticationError("未找到匹配的签名公钥")

        algorithms = [header.get("alg") or "RS256"]
        issuer = self.cfg.casdoor_issuer or self.base_url
        try:
            claims = jose_jwt.decode(
                token,
                jose_jwk.construct(key_data),
                algorithms=algorithms,
                issuer=self._issuer_variants(issuer),
                options={"verify_aud": False},
            )
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError(f"令牌签名校验失败: {exc}") from exc

        identity = self._identity_from_user_object(claims, auth_method="jwt")
        identity.raw = claims
        return identity

    def _issuer_variants(self, issuer: str) -> List[str]:
        """Casdoor 内部配置的 origin 常为 http，外部可用 https，两者均接受。"""
        variants = {issuer}
        if issuer.startswith("https://"):
            variants.add("http://" + issuer[len("https://") :])
        elif issuer.startswith("http://"):
            variants.add("https://" + issuer[len("http://") :])
        return sorted(variants)

    # ------------------------------------------------------------------
    # JWKS
    # ------------------------------------------------------------------
    async def _get_jwks(self) -> Dict[str, Any]:
        now = time.time()
        if self._jwks_cache and now - self._jwks_fetched_at < self.cfg.casdoor_jwks_cache_ttl:
            return self._jwks_cache
        async with self._lock:
            if self._jwks_cache and time.time() - self._jwks_fetched_at < self.cfg.casdoor_jwks_cache_ttl:
                return self._jwks_cache
            data = await self._get_json("/.well-known/jwks")
            if not isinstance(data, dict):
                raise CasdoorError("JWKS 响应格式异常")
            self._jwks_cache = data
            self._jwks_fetched_at = time.time()
            return data

    def _find_jwk(self, kid: Optional[str]) -> Optional[Dict[str, Any]]:
        keys = self._jwks_cache.get("keys", [])
        for key in keys:
            if kid is None or key.get("kid") == kid:
                return key
        # kid 未命中但 JWKS 只有一把公钥时回退使用（Casdoor 轮换证书期间可能短暂不一致）
        if len(keys) == 1:
            return keys[0]
        return None

    # ------------------------------------------------------------------
    # 服务令牌（client_credentials）与用户查询
    # ------------------------------------------------------------------
    async def get_service_token(self) -> Optional[str]:
        """用 client_credentials 换取服务令牌，用于调用 Casdoor 管理类接口。"""
        if not self.cfg.casdoor_has_client_credentials:
            return None
        now = time.time()
        if self._service_token and now < self._service_token_expires_at - 60:
            return self._service_token

        async with self._lock:
            now = time.time()
            if self._service_token and now < self._service_token_expires_at - 60:
                return self._service_token
            try:
                response = await self._post_form(
                    "/api/login/oauth/access_token",
                    form={"grant_type": "client_credentials", "scope": "openid profile email"},
                    basic_auth=True,
                )
            except CasdoorError as exc:
                logger.warning("获取 Casdoor 服务令牌失败：%s", exc)
                return None

            token = _first(response, "access_token", "accessToken")
            if not token:
                return None
            self._service_token = str(token)
            expires_in = int(response.get("expires_in") or 3600)
            self._service_token_expires_at = now + expires_in
            return self._service_token

    async def get_user(self, owner: str, user_id_or_name: str) -> Optional[Dict[str, Any]]:
        """按 owner + 用户 id/用户名查询 Casdoor 用户。"""
        service_token = await self.get_service_token()
        if not service_token:
            return None
        try:
            return await self._get_json(
                "/api/get-user",
                params={"owner": owner, "id": user_id_or_name},
                headers={"Authorization": f"Bearer {service_token}"},
            )
        except CasdoorError as exc:
            logger.warning("查询 Casdoor 用户失败：%s", exc)
            return None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        client = self._client()
        try:
            response = await client.get(self._url(path), params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise CasdoorError(f"访问 Casdoor 失败: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationError("访问令牌无效或权限不足")
        if response.status_code >= 500:
            raise CasdoorError(f"Casdoor 服务异常: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise CasdoorError("Casdoor 返回非 JSON 响应") from exc

    async def _post_form(
        self,
        path: str,
        form: Dict[str, Any],
        *,
        basic_auth: bool = False,
    ) -> Dict[str, Any]:
        client = self._client()
        headers: Dict[str, str] = {}
        auth = None
        if basic_auth:
            if not self.cfg.casdoor_has_client_credentials:
                raise AuthenticationError("缺少 Casdoor 客户端凭据")
            auth = (self.cfg.casdoor_client_id, self.cfg.casdoor_client_secret)
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            response = await client.post(self._url(path), data=form, headers=headers, auth=auth)
        except httpx.HTTPError as exc:
            raise CasdoorError(f"访问 Casdoor 失败: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code in (401, 403):
            raise AuthenticationError(str(payload.get("error_description") or "Casdoor 拒绝了本次请求"))
        if response.status_code >= 400:
            raise CasdoorError(str(payload.get("error_description") or f"HTTP {response.status_code}"))
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 身份构造
    # ------------------------------------------------------------------
    def _identity_from_user_object(self, obj: Dict[str, Any], *, auth_method: str) -> CasdoorIdentity:
        if not isinstance(obj, dict):
            raise AuthenticationError("用户信息格式异常")

        # Casdoor 对无效 Token 会返回 HTTP 200 + {"status":"error","msg":...}，
        # 必须显式识别，否则会被误判为认证成功。
        if obj.get("status") == "error":
            raise AuthenticationError(str(obj.get("msg") or "身份认证失败"))
        if obj.get("data") is None and "msg" in obj:
            raise AuthenticationError(str(obj.get("msg") or "身份认证失败"))

        # Casdoor 可能返回 {"status":"ok","data":{...}} 包装
        if isinstance(obj.get("data"), dict) and obj.get("data"):
            obj = obj["data"]

        if obj.get("isForbidden") is True:
            raise AuthenticationError("该账号已被禁用")

        user_id = _first(obj, "id", "sub", "user_id", "uid")
        username = _first(obj, "name", "preferred_username", "username", "login")
        if not user_id and not username:
            raise AuthenticationError("用户信息缺少唯一标识")

        properties = obj.get("properties") or {}
        tenant_id = str(
            _first(
                obj,
                "owner",
                "organization",
                "default",
                default=_first(properties, "tenant_id", "tenantId", "organization"),
            )
            or self.cfg.casdoor_organization
        )

        groups = [str(g) for g in (obj.get("groups") or [])]
        roles = [str(r) for r in (obj.get("roles") or [])]
        role = obj.get("role")
        role = str(role) if role else None

        identity = CasdoorIdentity(
            user_id=str(user_id or username),
            username=str(username or user_id),
            display_name=str(_first(obj, "displayName", "name", default=username) or username),
            tenant_id=tenant_id,
            email=_first(obj, "email"),
            phone=_first(obj, "phone"),
            role=role,
            groups=groups,
            roles=roles,
            properties=properties if isinstance(properties, dict) else {},
            auth_method=auth_method,
            raw=obj,
        )
        self._apply_admin_flags(identity)
        return identity

    def _apply_admin_flags(self, identity: CasdoorIdentity) -> None:
        """按配置判定管理员与跨组织管理员。"""
        cfg = self.cfg

        global_admin = bool(
            identity.role and identity.role in cfg.global_admin_roles
        ) or any(r in cfg.global_admin_roles for r in identity.roles)

        admin = global_admin
        if identity.role and identity.role in cfg.admin_roles:
            admin = True
        if any(r in cfg.admin_roles for r in identity.roles):
            admin = True
        if identity.raw.get("isAdmin") is True:
            admin = True
        if self._match_admin_groups(identity.groups, identity.tenant_id):
            admin = True

        identity.is_admin = admin
        identity.is_global_admin = global_admin

    def _match_admin_groups(self, groups: Sequence[str], tenant_id: str) -> bool:
        """管理员用户组匹配。

        支持两种写法：
        - ``admins``：命中当前组织下的同名用户组；
        - ``org:group``：命中指定组织的用户组（跨组织精确匹配）。
        """
        for raw in groups:
            group = str(raw)
            if group in self.cfg.admin_groups:
                return True
            for pattern in self.cfg.admin_groups:
                if ":" not in pattern:
                    continue
                target_org, _, target_group = pattern.partition(":")
                if target_group == group and (
                    target_org == tenant_id or target_org == "*"
                ):
                    return True
        return False


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------
def get_casdoor_client(request: Request) -> CasdoorClient:
    """从应用状态取出 Casdoor 客户端（由 lifespan 创建）。"""
    client = getattr(request.app.state, "casdoor_client", None)
    if client is None:  # pragma: no cover - 仅在 lifespan 未启用时触发
        client = CasdoorClient(global_settings)
        request.app.state.casdoor_client = client
    return client
