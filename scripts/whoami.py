#!/usr/bin/env python
"""身份与管理员判定自查工具。

用法：

    python scripts/whoami.py <access_token>
    python scripts/whoami.py --token-file token.txt

输出 Casdoor 解析出的身份、各判定项的命中情况，以及「若要成为管理员该怎么配」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.integrations.casdoor import CasdoorClient  # noqa: E402


async def main(token: str) -> int:
    client = CasdoorClient(settings)
    try:
        identity = await client.verify_token(token)
    except AppError as exc:
        print(f"[认证失败] {exc.message}")
        return 1
    finally:
        await client.aclose()

    print("=" * 68)
    print(f"用户 ID      : {identity.user_id}")
    print(f"用户名        : {identity.username}")
    print(f"显示名        : {identity.display_name}")
    print(f"所属企业      : {identity.tenant_id}")
    print(f"邮箱 / 手机   : {identity.email or '-'} / {identity.phone or '-'}")
    print(f"校验方式      : {identity.auth_method}")
    print("-" * 68)
    print(f"角色 roles    : {identity.roles or '（无）'}")
    print(f"用户组 groups : {identity.groups or '（无）'}")
    print(f"权限 permissions: {identity.permissions or '（无）'}")
    print("-" * 68)
    print(f"是否管理员    : {'是' if identity.is_admin else '否'}")
    print(f"是否跨组织管理员: {'是' if identity.is_global_admin else '否'}")
    print(f"判定依据      : {identity.admin_reason or '（未命中任何判定条件）'}")

    if not identity.is_admin:
        print("-" * 68)
        print("当前判定配置：")
        print(f"  CASDOOR_ADMIN_ROLES       = {sorted(settings.admin_roles)}")
        print(f"  CASDOOR_ADMIN_GROUPS      = {sorted(settings.admin_groups)}")
        print(f"  CASDOOR_ADMIN_PERMISSIONS = {sorted(settings.admin_permissions)}")
        print(f"  CASDOOR_ADMIN_USERS       = {sorted(settings.admin_users)}")
        print("  CASDOOR_GLOBAL_ADMIN_ROLES= {sorted(settings.global_admin_roles)}")
        print()
        print("可行的授权方式（任选其一）：")
        print(f"  A. 在 Casdoor 给 {identity.username} 添加角色"
              f"（名称需在 CASDOOR_ADMIN_ROLES 中），或加入管理员用户组")
        print("  B. 在 Casdoor 建 Permission 并授权，再配置 CASDOOR_ADMIN_PERMISSIONS=<权限名>")
        print(f"  C. 配置 CASDOOR_ADMIN_USERS={identity.username}（最省事）")

    print("=" * 68)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Casdoor 身份与管理员判定自查")
    parser.add_argument("token", nargs="?", help="Access Token")
    parser.add_argument("--token-file", help="从文件读取 Access Token")
    args = parser.parse_args()

    token = args.token
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        parser.error("请提供 Access Token（位置参数或 --token-file）")

    raise SystemExit(asyncio.run(main(token)))
