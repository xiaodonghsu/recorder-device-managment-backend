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
    # clientId 是公开标识符（会出现在前端 OAuth 跳转 URL 里），可安全地内置默认值；
    # clientSecret 是真正的机密，只能通过环境变量 / .env 提供，不要写进代码。
    casdoor_client_id: str = "6932b8ce0b655b9421b5"
    casdoor_client_secret: str = ""
    # Casdoor 后台「组织 / 应用」标识（2026-11-08 改名后同步）：
    #   owner（组织）= bestlink，name（应用）= voicehub，组合即 bestlink/voicehub。
    # 组织名同时是员工的 owner，也就是本服务的 tenant_id；改名会改变租户范围，
    # 存量数据的 tenant_id 需另行迁移，否则旧设备对新租户不可见。
    casdoor_organization: str = "bestlink"
    # 应用名用于识别 Casdoor 应用与后端 /auth/config 的对外声明；OAuth 授权
    # 实际以 client_id 为准，因此应用改名后请务必核对 clientId 是否变化。
    casdoor_application: str = "voicehub"
    casdoor_verify_method: str = "auto"
    casdoor_timeout: float = 10.0
    casdoor_verify_tls: bool = True
    casdoor_jwks_cache_ttl: int = 3600
    # 领用时是否向 Casdoor 校验工号（preferred_username）真实存在：
    #   off      —— 不校验，完全离线（仅供测试 / 无 Casdoor 环境）
    #   optional —— 能查就查；查到「工号不存在 / 组织不符 / 账号禁用」则拒绝，
    #               但 Casdoor 不可达或接口无权限时降级放行并告警（默认，可用性优先）
    #   required —— 连查询都做不到时也拒绝分配，适合要求强一致的生产环境
    # 本实例的 /api/get-user 只读接口无需令牌，因此不配 clientSecret 也能真正校验；
    # 若 Casdoor 收紧了权限，可配 CASDOOR_CLIENT_SECRET 走服务令牌，或把请求方
    # 管理员的 access token 透传过去（见 CasdoorClient.lookup_employee）。
    casdoor_verify_employee: str = "optional"

    # Casdoor 内置管理员角色的 name 通常是 `role_admin`（后台角色列表里带 role_ 前缀），
    # 自建角色则常用 `admin`。三种命名都纳入默认白名单，避免"明明是管理员却进不去后台"。
    # 用户组只需写短名：Casdoor 返回的 `bestlink/admins` 会在匹配时自动剥离组织前缀。
    casdoor_admin_roles: str = "global_admin,admin,role_admin"
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

    # ---------------- 管理后台静态资源 ----------------
    serve_frontend: bool = True
    """是否由本服务托管 ../frontend 目录（访问路径 /admin）。"""

    frontend_dir: str = ""
    """管理后台静态目录绝对路径；留空则使用项目根目录下的 frontend/。"""

    @field_validator("casdoor_verify_method")
    @classmethod
    def _check_verify_method(cls, v: str) -> str:
        allowed = {"auto", "introspect", "userinfo", "jwt"}
        value = (v or "auto").strip().lower()
        if value not in allowed:
            raise ValueError(f"casdoor_verify_method 必须为 {sorted(allowed)} 之一")
        return value

    @field_validator("casdoor_verify_employee")
    @classmethod
    def _check_verify_employee(cls, v: str) -> str:
        allowed = {"off", "optional", "required"}
        value = (v or "optional").strip().lower()
        if value not in allowed:
            raise ValueError(f"casdoor_verify_employee 必须为 {sorted(allowed)} 之一")
        return value

    @field_validator("api_prefix")
    @classmethod
    def _check_api_prefix(cls, v: str) -> str:
        """规范化接口前缀。

        两个常见坑：

        1. **漏掉前导斜杠**：写成 ``voicehub-api``。FastAPI 的
           ``include_router(prefix=...)`` 要求前缀以 ``/`` 开头，
           否则会在导入阶段抛断言错误，这里统一补全。
        2. **被 shell 转换成 Windows 路径**：在 Git Bash / MSYS 下执行
           ``API_PREFIX=/voicehub-api python ...``，MSYS 会把以 ``/`` 开头的值
           自动转换成 ``C:/Program Files/Git/voicehub-api``，导致所有接口
           挂在错误路径上。这里显式拦截并给出可执行的修复提示。
        """
        value = (v or "").strip()
        if not value:
            return "/api"
        if ":" in value or "\\" in value:
            raise ValueError(
                "api_prefix 不应是文件路径（检测到 ':' 或 '\\'）。"
                "在 Git Bash / MSYS 下直接写 `API_PREFIX=/voicehub-api` 会被自动"
                "转换成 Windows 绝对路径，请改用 backend/.env 文件配置，"
                "或执行 `export MSYS_NO_PATHCONV=1` 后再设置环境变量。"
            )
        if not value.startswith("/"):
            value = "/" + value
        value = value.rstrip("/")
        return value or "/api"

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
    def frontend_path(self) -> Path:
        """管理后台静态目录。"""
        if self.frontend_dir.strip():
            return Path(self.frontend_dir).expanduser().resolve()
        return (BASE_DIR.parent / "frontend").resolve()

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
