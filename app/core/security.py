"""设备凭证（AuthKey）与通用安全工具。

设计要点（README §3.2 / §7）：
- AuthKey 使用高强度随机值生成，数据库只保存 HMAC-SHA256 摘要，明文永不落库；
- 摘要使用服务端密钥保护，密钥泄露不会直接暴露设备凭证；
- 校验采用常量时间比较，防止时序侧信道；
- AuthKey 与员工 Access Token 完全独立，此处不涉及任何用户令牌逻辑。
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from typing import Optional

from app.core.config import settings

# AuthKey 长度：32 字节随机值，URL-safe 编码后约 43 字符
AUTH_KEY_BYTES = 32
AUTH_KEY_PREFIX = "vhk"


def generate_auth_key() -> str:
    """生成高强度随机 AuthKey（明文，仅在入库/轮换时一次性返回给管理员）。"""
    return f"{AUTH_KEY_PREFIX}_{secrets.token_urlsafe(AUTH_KEY_BYTES)}"


def hash_auth_key(auth_key: str, *, secret: Optional[str] = None) -> str:
    """计算 AuthKey 的安全摘要：HMAC-SHA256(server_secret, auth_key)。"""
    key = (secret or settings.auth_key_secret).encode("utf-8")
    return hmac.new(key, auth_key.encode("utf-8"), sha256).hexdigest()


def verify_auth_key(auth_key: str, auth_key_hash: str, *, secret: Optional[str] = None) -> bool:
    """常量时间比对 AuthKey 与其摘要。"""
    if not auth_key or not auth_key_hash:
        return False
    return hmac.compare_digest(hash_auth_key(auth_key, secret=secret), auth_key_hash)


def is_auth_key_plausible(auth_key: str) -> bool:
    """轻量格式校验，避免对明显非法的输入做摘要计算。"""
    if not auth_key or not (16 <= len(auth_key) <= 128):
        return False
    return all(ch.isalnum() or ch in "_-" for ch in auth_key)
