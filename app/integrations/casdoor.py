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
from typing import Any, Dict, List, Optional, Sequence, Set

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
    """与业务无关的已认证身份。

    三个标识必须严格区分（Casdoor ``/api/userinfo`` 返回的是 OIDC claims）：

    ======================  ==========================  ==========================
    属性                     来源 claim                  用途
    ======================  ==========================  ==========================
    ``user_id``             ``sub`` / ``id``（UUID）     技术标识：审计、兼容历史记录
    ``username``            ``preferred_username``       工号：业务侧员工唯一标识
    ``display_name``        ``name`` / ``displayName``   姓名：仅用于界面展示
    ======================  ==========================  ==========================

    业务表 ``device_assignment.user_id`` / ``device_binding.user_id`` 存的是
    **工号**；姓名可能重名，禁止参与身份匹配。
    """

    user_id: str
    username: str
    display_name: str
    tenant_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    is_admin: bool = False
    is_global_admin: bool = False
    admin_reason: List[str] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    auth_method: str = "unknown"
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def employee_no(self) -> str:
        """业务侧员工唯一标识：工号（preferred_username）。

        与 ``user_id``（技术 UUID）不同。旧版本 Token 若没有 preferred_username，
        回退到 UUID，保证该值恒非空且不会误用姓名。
        """
        return self.username or self.user_id


def _first(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return default


@dataclass(frozen=True)
class EmployeeLookup:
    """工号存在性查询结果，必须是三态而不能只用 bool。

    ==================  ==========================================================
    ``status``           语义
    ==================  ==========================================================
    ``found``            Casdoor 中存在该工号，且组织一致、账号未禁用
    ``absent``           明确查无此工号 / 组织不符 / 账号禁用 → 应拒绝分配
    ``unavailable``      Casdoor 不可达、接口无权限、响应无法解析 → 不能据此下结论
    ==================  ==========================================================

    把「查询不可用」当成「工号不存在」会造成大面积误杀：Casdoor 一抖动，
    所有设备都分配不出去。因此两者必须分开，由 ``casdoor_verify_employee``
    策略决定 ``unavailable`` 是降级放行还是拒绝。
    """

    status: str
    employee_no: str = ""
    display_name: str = ""
    owner: str = ""
    user_id: str = ""
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.status == "found"


def _extract_names(raw: Any) -> List[str]:
    """从 Casdoor 返回的数组字段中提取名称列表。

    Casdoor 的三种数组字段形态不一致：
    - ``groups``      → ``[]string``
    - ``roles``       → ``[]*Role``，名字在 ``Role.Name``
    - ``permissions`` → ``[]*Permission``，名字在 ``Permission.Name``

    旧版本 Casdoor 的 ``roles`` 可能是字符串数组，这里一并兼容。
    """
    names: List[str] = []
    if not raw:
        return names
    if isinstance(raw, str):
        return [raw]
    for item in raw:
        if isinstance(item, str):
            if item:
                names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("role") or item.get("displayName")
            if name:
                names.append(str(name))
    return names


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
        username = _first(response, "preferred_username", "username", "name")
        display_name = _first(response, "name", "displayName", default=username)
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
            display_name=str(display_name or username or sub or ""),
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

    @staticmethod
    def _unwrap_user(payload: Any) -> Optional[Dict[str, Any]]:
        """兼容 Casdoor 的 ``{"status":"ok","data":{...}}`` 包装与裸用户对象。"""
        if not isinstance(payload, dict) or payload.get("status") == "error":
            return None
        data = payload.get("data")
        if isinstance(data, dict) and data and data.get("id"):
            return data
        if payload.get("id"):
            return payload
        return None

    async def _user_query_tokens(self, access_token: Optional[str]) -> List[Optional[str]]:
        """构造查询 ``/api/get-user`` 时依次尝试的身份。

        1. 服务令牌（需 CASDOOR_CLIENT_ID + CASDOOR_CLIENT_SECRET，最稳定）；
        2. 当前请求管理员的 access token（Casdoor 收紧只读权限时的兜底）；
        3. 匿名（本实例的 ``get-user`` 是公开只读接口，实测无需令牌即可查询，
           这样不配 clientSecret 也能在领用时真正校验工号）。

        ``None`` 表示不带 Authorization 头。
        """
        tokens: List[Optional[str]] = []
        service_token = await self.get_service_token()
        if service_token:
            tokens.append(service_token)
        if access_token and access_token.strip() and access_token not in tokens:
            tokens.append(access_token.strip())
        tokens.append(None)
        return tokens

    async def _fetch_user(
        self,
        owner: str,
        candidate: str,
        tokens: List[Optional[str]],
    ) -> tuple[Optional[Dict[str, Any]], str]:
        """返回 (Casdoor 响应, 失败原因)。任一身份拿到明确响应即停止尝试。"""
        last_error = ""
        for token in tokens:
            headers = {"Authorization": f"Bearer {token}"} if token else None
            try:
                payload = await self._get_json(
                    "/api/get-user",
                    params={"owner": owner, "id": candidate},
                    headers=headers,
                )
            except (AuthenticationError, CasdoorError) as exc:
                last_error = str(getattr(exc, "message", exc))
                continue
            # status=error 常见于「该身份无权查询」或 id 格式不对；换身份再试一次
            if isinstance(payload, dict) and payload.get("status") == "error":
                last_error = str(payload.get("msg") or "Casdoor 拒绝查询")
                continue
            return payload, ""
        return None, last_error or "Casdoor 未返回用户查询响应"

    @staticmethod
    def _classify_user_payload(
        payload: Optional[Dict[str, Any]],
        tenant_id: str,
        requested: str,
        *,
        fallback_reason: str = "",
    ) -> EmployeeLookup:
        """把 ``/api/get-user`` 响应归类为 found / absent / unavailable。

        两个实测坑：

        - 查询参数里的 ``owner`` 会被 Casdoor 忽略（``id`` 才是唯一依据），
          所以必须校验**返回对象**的 ``owner``，否则别的组织的工号会被当成存在；
        - 工号大小写不敏感（``jh000059`` 也返回 ``JH000059``），因此写入业务表的
          必须是 Casdoor 返回的规范 ``name``，而不是管理员输入的原值。
        """
        if not isinstance(payload, dict):
            return EmployeeLookup("unavailable", reason=fallback_reason or "用户查询无响应")
        if payload.get("status") == "error":
            return EmployeeLookup(
                "unavailable", reason=str(payload.get("msg") or fallback_reason or "Casdoor 查询失败")
            )

        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            data = payload if payload.get("id") else None

        if isinstance(data, dict) and data.get("id"):
            owner = str(data.get("owner") or "")
            if owner and owner != tenant_id:
                return EmployeeLookup(
                    "absent", owner=owner, reason=f"该用户属于组织 {owner}，不属于 {tenant_id}"
                )
            if data.get("isForbidden") is True or data.get("isDeleted") is True or data.get("deletedTime"):
                return EmployeeLookup(
                    "absent", owner=owner, reason="该账号已禁用或已删除"
                )
            employee_no = str(
                _first(data, "name", "preferred_username", "preferredUsername", "username") or ""
            )
            if not employee_no:
                return EmployeeLookup("unavailable", reason="用户对象缺少工号字段（name）")
            return EmployeeLookup(
                "found",
                employee_no=employee_no,
                display_name=str(_first(data, "displayName", "display_name", default="") or ""),
                owner=owner,
                user_id=str(data["id"]),
            )

        if payload.get("data") is None and payload.get("status") == "ok":
            return EmployeeLookup("absent", reason=f"组织 {tenant_id} 中不存在工号 {requested!r}")
        return EmployeeLookup("unavailable", reason=fallback_reason or "无法解析 Casdoor 用户响应")

    async def lookup_employee(
        self,
        owner: str,
        employee_no: str,
        *,
        access_token: Optional[str] = None,
    ) -> EmployeeLookup:
        """校验工号（Casdoor 用户名 / ``preferred_username``）是否真实可用。

        只查 ``id=<owner>/<工号>`` 这一种形式：实测裸工号与裸 UUID 都会被
        Casdoor 判为 ``GetOwnerAndNameFromId() error``，查 UUID 需要管理员令牌
        拉全量用户，成本高且权限不一定有，因此 UUID 输入在业务层直接拒绝。
        """
        value = (employee_no or "").strip()
        if not value:
            return EmployeeLookup("absent", reason="工号不能为空")
        candidate = value if "/" in value else f"{owner}/{value}"
        tokens = await self._user_query_tokens(access_token)
        payload, error = await self._fetch_user(owner, candidate, tokens)
        result = self._classify_user_payload(payload, owner, value, fallback_reason=error)
        if result.status == "unavailable":
            logger.warning("工号 %r 校验查询不可用（tenant=%s）：%s", value, owner, result.reason)
        return result

    async def get_user(
        self,
        owner: str,
        user_id_or_name: str,
        *,
        access_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 owner + 用户 UUID / 用户名查询 Casdoor 用户（兼容旧调用）。

        不再强依赖服务令牌：与 ``lookup_employee`` 共用身份链。
        """
        value = (user_id_or_name or "").strip()
        if not value:
            return None
        candidates = [value] if "/" in value else [f"{owner}/{value}", value]
        tokens = await self._user_query_tokens(access_token)
        for candidate in candidates:
            payload, error = await self._fetch_user(owner, candidate, tokens)
            if payload is None:
                logger.debug("查询 Casdoor 用户失败（id=%s）：%s", candidate, error)
                continue
            user = self._unwrap_user(payload)
            if user:
                return user
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

        # /api/userinfo 返回 OIDC claims，/api/get-user 返回 Casdoor 用户对象，
        # 两者对 `name` 的定义恰好相反，必须按来源分别解析：
        #   OIDC claims : sub=UUID, preferred_username=工号, name=姓名
        #   用户对象     : id=UUID,  name=工号,               displayName=姓名
        # 若统一按 "name" 取用户名，就会把「胥晓冬」当成员工标识，
        # 导致领用记录里的工号 JH000059 永远匹配不上登录身份。
        oidc_claims = "preferred_username" in obj or ("sub" in obj and "id" not in obj)
        user_id = _first(obj, "id", "sub", "user_id", "uid")
        if oidc_claims:
            username = _first(obj, "preferred_username", "username", "login", "name")
            display_name = _first(obj, "name", "displayName", "display_name", default=username)
        else:
            username = _first(obj, "name", "preferred_username", "username", "login")
            display_name = _first(obj, "displayName", "display_name", "name", default=username)
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

        groups = _extract_names(obj.get("groups"))
        roles = _extract_names(obj.get("roles"))
        permissions = _extract_names(obj.get("permissions"))
        # 旧版 Casdoor 的 User.role 是单个字符串角色名
        role: Optional[str] = None
        legacy_role = obj.get("role")
        if legacy_role and isinstance(legacy_role, str):
            role = legacy_role
            roles.insert(0, legacy_role)

        identity = CasdoorIdentity(
            user_id=str(user_id or username),
            username=str(username or user_id),
            display_name=str(display_name or username or user_id),
            tenant_id=tenant_id,
            email=_first(obj, "email"),
            phone=_first(obj, "phone"),
            role=role,
            groups=groups,
            roles=roles,
            permissions=permissions,
            properties=properties if isinstance(properties, dict) else {},
            auth_method=auth_method,
            raw=obj,
        )
        self._apply_admin_flags(identity)
        return identity

    def _apply_admin_flags(self, identity: CasdoorIdentity) -> None:
        """按配置判定管理员与跨组织管理员。

        判定路径（任一命中即为管理员），并记录命中依据便于排障：

        1. 角色命中 ``CASDOOR_GLOBAL_ADMIN_ROLES`` → 跨组织管理员
        2. 角色命中 ``CASDOOR_ADMIN_ROLES``
        3. Casdoor 用户属性 ``isAdmin == true``
        4. 用户组命中 ``CASDOOR_ADMIN_GROUPS``
        5. 权限命中 ``CASDOOR_ADMIN_PERMISSIONS``（Casdoor 官方 RBAC 用法）
        6. 用户名 / 邮箱命中 ``CASDOOR_ADMIN_USERS``
        """
        cfg = self.cfg
        reasons: List[str] = []
        role_names = set(identity.roles)

        global_hit = role_names & cfg.global_admin_roles
        if global_hit:
            identity.is_global_admin = True
            reasons.append(f"角色命中全局管理员：{sorted(global_hit)}")

        admin_hit = role_names & cfg.admin_roles
        if admin_hit:
            reasons.append(f"角色命中管理员：{sorted(admin_hit)}")

        if identity.raw.get("isAdmin") is True:
            reasons.append("Casdoor 用户属性 isAdmin=true")

        matched_groups = self._match_admin_group_names(identity.groups, identity.tenant_id)
        if matched_groups:
            reasons.append(f"用户组命中管理员组：{sorted(matched_groups)}")

        matched_perms = sorted(set(identity.permissions) & cfg.admin_permissions)
        if matched_perms:
            reasons.append(f"Casdoor 权限命中：{matched_perms}")

        if self._match_admin_user(identity):
            reasons.append(f"用户名/邮箱命中管理员白名单：{identity.username}")

        identity.admin_reason = reasons
        identity.is_admin = bool(reasons)
        identity.is_global_admin = identity.is_global_admin or bool(global_hit)

    def _match_admin_user(self, identity: CasdoorIdentity) -> bool:
        """用户名 / 邮箱白名单匹配（兜底方案）。"""
        if not self.cfg.admin_users:
            return False
        candidates = {
            str(identity.username).lower(),
            str(identity.user_id).lower(),
            str(identity.email or "").lower(),
        }
        return bool(candidates & self.cfg.admin_users)

    def _match_admin_group_names(
        self, groups: Sequence[str], tenant_id: str
    ) -> Set[str]:
        """返回命中的管理员用户组名（用于排障展示的原始名称）。

        Casdoor 的 ``User.groups`` 是 ``[]string``，取值通常是**全限定名**
        ``owner/group``（例如 ``bestlink/admins``），而配置里习惯写短名
        ``admins``。若只做字符串精确比较，明明在管理员组里的用户也会被判为非管理员。
        因此这里把每个组拆成「组织段 + 短名」再比对，规则如下：

        - 配置写 ``admins``：命中同名组（无论是否带组织前缀）；
        - 配置写 ``tenant/admins`` 或 ``tenant:admins``：精确匹配该组织的组；
        - 配置写 ``*/admins``：通配任意组织；
        - 配置直接写全限定名（如 ``bestlink/admins``）：与返回值精确匹配。

        短名不做组织校验是有意为之：Casdoor 组织改名期间，Token 里的 ``owner``
        可能与用户组前缀暂时不一致；且 ``is_admin`` 只决定**本租户内**的操作权限，
        数据可见范围始终由服务端推导的 ``tenant_id`` 限定，放宽不会造成越权。
        """
        matched: Set[str] = set()
        patterns = {str(p).strip() for p in self.cfg.admin_groups if str(p).strip()}
        if not patterns:
            return matched

        for raw in groups:
            group = str(raw).strip()
            if not group:
                continue
            # 兼容旧版本可能使用的 `org:group` 写法
            normalized = group.replace(":", "/", 1)
            owner, sep, short = normalized.rpartition("/")
            if not sep:
                owner, short = tenant_id, normalized
            elif not owner:
                owner = tenant_id
            if not short:
                continue

            for pattern in patterns:
                # ① 全限定名精确匹配
                if pattern == group or pattern.replace(":", "/", 1) == normalized:
                    matched.add(group)
                    continue

                p_owner, p_sep, p_short = pattern.replace(":", "/", 1).rpartition("/")
                if not p_sep or not p_short:
                    # ② 短名：命中任意组织前缀下的同名组
                    if pattern == short:
                        matched.add(group)
                    continue

                # ③ 带组织段的精确匹配
                if p_short == short and (p_owner in (tenant_id, "*") or p_owner == owner):
                    matched.add(group)
        return matched


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
