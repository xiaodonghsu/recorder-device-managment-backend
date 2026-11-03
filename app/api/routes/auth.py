"""认证辅助接口：暴露 Casdoor 登录参数与当前登录态。"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["认证"])


@router.get(
    "/auth/config",
    summary="获取 Casdoor 登录配置",
    description=(
        "返回 APP / 管理后台发起 Casdoor 授权码登录所需的参数。"
        "Casdoor 实例配置了 authPrefix=auth，因此授权地址需带 /auth 前缀。"
    ),
)
async def auth_config() -> Dict[str, Any]:
    base = settings.casdoor_base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/login/oauth/authorize",
        "token_endpoint": f"{base}/api/login/oauth/access_token",
        "userinfo_endpoint": f"{base}/api/userinfo",
        "introspection_endpoint": f"{base}/api/login/oauth/introspect",
        "jwks_uri": f"{base}/.well-known/jwks",
        "client_id": settings.casdoor_client_id,
        "organization": settings.casdoor_organization,
        "scopes": ["openid", "profile", "email"],
        "grant_types": ["authorization_code", "refresh_token"],
    }
