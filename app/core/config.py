"""应用配置中心。

所有配置项均可通过环境变量或 backend/.env 覆盖。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Set

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 应用 ----------------
    app_name: str = "嘉环声枢录音卡设备管理服务"
    app_version: str = "1.0.0"
    app_env: str = "development"
    debug: bool = False
    api_prefix: str = "/api"

    # ---------------- 数据库 ----------------
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'dev.db').as_posix()}"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600

    # ---------------- Casdoor ----------------
    casdoor_base_url: str = "https://office.uassist.cn:40000/auth"
    casdoor_issuer: str = ""
    casdoor_client_id: str = ""
    casdoor_client_secret: str = ""
    casdoor_organization: str = "built-in"
    casdoor_verify_method: str = "auto"
    casdoor_timeout: float = 10.0
    casdoor_verify_tls: bool = True
    casdoor_jwks_cache_ttl: int = 3600

    casdoor_admin_roles: str = "global_admin,admin"
    casdoor_admin_groups: str = "admins"
    casdoor_global_admin_roles: str = "global_admin"
    # Casdoor Permission 名称白名单：在 Casdoor 后台为应用创建权限后，
    # 将权限的 name 填到这里，命中的用户即为管理员（Casdoor 官方 RBAC 用法）
    casdoor_admin_permissions: str = ""
    # 用户名 / 邮箱白名单（兜底方案，不依赖 Casdoor 内部权限配置）
    casdoor_admin_users: str = ""

    # ---------------- 设备凭证 ----------------
    auth_key_secret: str = "please-change-this-to-a-long-random-secret"
    auth_key_default_ttl_days: int = 0
    device_online_timeout_seconds: int = 300

    # ---------------- CORS ----------------
    cors_origins: str = "*"

    @field_validator("casdoor_verify_method")
    @classmethod
    def _check_verify_method(cls, v: str) -> str:
        allowed = {"auto", "introspect", "userinfo", "jwt"}
        value = (v or "auto").strip().lower()
        if value not in allowed:
            raise ValueError(f"casdoor_verify_method 必须为 {sorted(allowed)} 之一")
        return value

    @field_validator("casdoor_base_url")
    @classmethod
    def _strip_base_url(cls, v: str) -> str:
        return (v or "").rstrip("/")

    # ---------------- 派生属性 ----------------
    @property
    def admin_roles(self) -> Set[str]:
        return {r.strip() for r in self.casdoor_admin_roles.split(",") if r.strip()}

    @property
    def admin_groups(self) -> Set[str]:
        return {g.strip() for g in self.casdoor_admin_groups.split(",") if g.strip()}

    @property
    def global_admin_roles(self) -> Set[str]:
        return {r.strip() for r in self.casdoor_global_admin_roles.split(",") if r.strip()}

    @property
    def admin_permissions(self) -> Set[str]:
        return {p.strip() for p in self.casdoor_admin_permissions.split(",") if p.strip()}

    @property
    def admin_users(self) -> Set[str]:
        return {u.strip().lower() for u in self.casdoor_admin_users.split(",") if u.strip()}

    @property
    def cors_origin_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def casdoor_has_client_credentials(self) -> bool:
        return bool(self.casdoor_client_id and self.casdoor_client_secret)

    def validate_runtime(self) -> List[str]:
        """返回生产环境下的风险项清单（空列表表示安全）。"""
        warnings: List[str] = []
        if self.app_env.lower() == "production":
            if self.auth_key_secret.startswith("please-change") or len(self.auth_key_secret) < 32:
                warnings.append("AUTH_KEY_SECRET 未设置或过短，生产环境必须使用 32 位以上随机值")
            if not self.casdoor_has_client_credentials:
                warnings.append("CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET 未配置，Token 校验将降级为 userinfo 模式")
            if self.casdoor_base_url.startswith("http://"):
                warnings.append("CASDOOR_BASE_URL 使用明文 http，生产环境建议启用 https")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
