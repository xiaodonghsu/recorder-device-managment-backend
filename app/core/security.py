"""设备 AuthKey 的随机生成与格式校验。"""

from __future__ import annotations

import secrets

# AuthKey 长度：32 字节随机值，URL-safe 编码后约 43 字符
AUTH_KEY_BYTES = 32
AUTH_KEY_PREFIX = "vhk"


def generate_auth_key() -> str:
    """生成高强度随机 AuthKey。"""
    return f"{AUTH_KEY_PREFIX}_{secrets.token_urlsafe(AUTH_KEY_BYTES)}"


def is_auth_key_plausible(auth_key: str) -> bool:
    """检查导入的 AuthKey 格式。"""
    if not auth_key or not (16 <= len(auth_key) <= 128):
        return False
    return all(ch.isalnum() or ch in "_-" for ch in auth_key)
