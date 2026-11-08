"""Casdoor 集成层单元测试。

使用 httpx.MockTransport 模拟 Casdoor 响应，验证：
- userinfo 返回的完整用户对象能正确映射为身份；
- introspect 模式能识别失效 Token；
- 管理员判定（isAdmin / role / groups）；
- 被禁用账号被拒绝登录。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.integrations.casdoor import CasdoorClient

BASE = "https://casdoor.test/auth"

CASDOOR_USER_OBJECT = {
    "owner": "acme",
    "name": "zhangsan",
    "id": "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c",
    "type": "normal-user",
    "displayName": "张三",
    "email": "zhangsan@example.com",
    "phone": "13800000000",
    "isAdmin": False,
    "isForbidden": False,
    "role": "",
    "groups": ["acme"],
    "roles": [],
    "properties": {},
    "signupApplication": "app-acme",
}


def build_client(handler) -> CasdoorClient:
    cfg = Settings(
        casdoor_base_url=BASE,
        casdoor_client_id="app-acme",
        casdoor_client_secret="secret",
        casdoor_organization="acme",
        casdoor_verify_method="auto",
    )
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return CasdoorClient(cfg, http_client)


def test_userinfo_maps_casdoor_user_to_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/api/userinfo"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json=CASDOOR_USER_OBJECT)

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("test-token"))

    assert identity.user_id == "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c"
    assert identity.username == "zhangsan"
    assert identity.display_name == "张三"
    assert identity.tenant_id == "acme"
    assert identity.email == "zhangsan@example.com"
    assert identity.auth_method == "userinfo"
    assert identity.is_admin is False


def test_admin_flag_from_role():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(CASDOOR_USER_OBJECT, role="admin")
        return httpx.Response(200, json=payload)

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is True
    assert identity.is_global_admin is False


def test_admin_flag_from_isadmin_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(CASDOOR_USER_OBJECT, isAdmin=True))

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is True


def test_admin_flag_from_group():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(CASDOOR_USER_OBJECT, groups=["admins"]))

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is True


def test_global_admin_role():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(CASDOOR_USER_OBJECT, role="global_admin"))

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_global_admin is True
    assert identity.is_admin is True


def test_forbidden_user_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(CASDOOR_USER_OBJECT, isForbidden=True))

    client = build_client(handler)
    with pytest.raises(AuthenticationError):
        asyncio.run(client.verify_token("t"))


def test_introspect_mode_detects_inactive_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/api/login/oauth/introspect"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"active": False})

    client = build_client(handler)
    client.cfg.casdoor_verify_method = "introspect"
    with pytest.raises(AuthenticationError):
        asyncio.run(client.verify_token("expired-token"))


def test_introspect_mode_accepts_active_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/introspect"):
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "sub": "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c",
                    "username": "zhangsan",
                    "client_id": "app-acme",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404, json={})

    client = build_client(handler)
    client.cfg.casdoor_verify_method = "introspect"
    identity = asyncio.run(client.verify_token("valid-token"))
    assert identity.user_id == "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c"
    assert identity.tenant_id == "acme"


def test_auth_config_endpoint(client):
    from app.core.config import settings

    response = client.get("/api/auth/config")
    assert response.status_code == 200
    body = response.json()
    assert body["authorization_endpoint"].endswith("/auth/login/oauth/authorize")
    assert body["token_endpoint"].endswith("/auth/api/login/oauth/access_token")
    assert body["userinfo_endpoint"].endswith("/auth/api/userinfo")
    assert "authorization_code" in body["grant_types"]
    # 组织 / 应用标识必须与服务端配置一致（改名后由这一处下发）
    assert body["organization"] == settings.casdoor_organization
    assert body["application"] == settings.casdoor_application


def test_jwt_mode_verifies_signature_with_jwks():
    """JWT 本地验签模式：使用自签 RSA 密钥构造 Token 与 JWKS 端点。"""
    import time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk as jose_jwk
    from jose import jwt as jose_jwt

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_jwk = dict(jose_jwk.construct(private_pem, algorithm="RS256").to_dict())
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})

    token = jose_jwt.encode(
        {
            "sub": "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c",
            "id": "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c",
            "name": "zhangsan",
            "displayName": "张三",
            "owner": "acme",
            "groups": ["acme"],
            "role": "admin",
            "iss": BASE,
            "exp": int(time.time()) + 600,
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/jwks"):
            return httpx.Response(200, json={"keys": [public_jwk]})
        return httpx.Response(404, json={})

    client = build_client(handler)
    client.cfg.casdoor_verify_method = "jwt"
    identity = asyncio.run(client.verify_token(token))

    assert identity.user_id == "3f0a9c1e-2b7d-4f21-9c3a-1d2e3f4a5b6c"
    assert identity.tenant_id == "acme"
    assert identity.is_admin is True
    assert identity.auth_method == "jwt"


# ---------------------------------------------------------------------------
# 管理员判定（Casdoor 的 roles / permissions 均为对象数组）
# ---------------------------------------------------------------------------
def test_roles_object_array_is_parsed_to_names():
    """Casdoor 的 roles 是 []*Role 对象数组，角色名取 Role.name。"""
    payload = dict(CASDOOR_USER_OBJECT)
    payload.pop("role", None)
    payload["roles"] = [
        {"owner": "acme", "name": "admin", "displayName": "管理员"},
        {"owner": "acme", "name": "auditor", "displayName": "审计员"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.roles == ["admin", "auditor"]
    assert identity.is_admin is True


def test_permissions_object_array_is_parsed_to_names():
    """Casdoor 的 permissions 是 []*Permission 对象数组，权限名取 Permission.name。"""
    payload = dict(CASDOOR_USER_OBJECT)
    payload["permissions"] = [
        {"owner": "acme", "name": "device-admin", "resources": ["*"], "actions": ["*"]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = build_client(handler)
    client.cfg.casdoor_admin_permissions = "device-admin"
    identity = asyncio.run(client.verify_token("t"))
    assert identity.permissions == ["device-admin"]
    assert identity.is_admin is True
    assert any("Casdoor 权限命中" in r for r in identity.admin_reason)


def test_admin_users_whitelist_fallback():
    payload = dict(CASDOOR_USER_OBJECT)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = build_client(handler)
    client.cfg.casdoor_admin_users = "zhangsan"
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is True

    client.cfg.casdoor_admin_users = "someone-else"
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is False
    assert identity.admin_reason == []


def test_non_admin_reports_diagnostics_in_403():
    """403 响应要带上角色 / 用户组 / 权限，便于自助定位。"""
    payload = dict(CASDOOR_USER_OBJECT)
    payload["groups"] = ["acme"]
    payload["roles"] = [{"owner": "acme", "name": "auditor"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = build_client(handler)
    identity = asyncio.run(client.verify_token("t"))
    assert identity.is_admin is False
    assert identity.roles == ["auditor"]
    assert identity.groups == ["acme"]
