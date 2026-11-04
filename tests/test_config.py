"""配置中心相关测试。

重点覆盖 ``api_prefix`` 的规范化：部署时这个值来自环境变量，
写错（漏前导斜杠）或被 shell 转换成 Windows 路径都会让接口整体不可用。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/voicehub-api", "/voicehub-api"),
        ("voicehub-api", "/voicehub-api"),  # 漏掉前导斜杠 → 自动补全
        ("/voicehub-api/", "/voicehub-api"),  # 尾部斜杠 → 自动去掉
        ("/api", "/api"),
        ("", "/api"),  # 空值 → 回退默认
    ],
)
def test_api_prefix_is_normalized(raw: str, expected: str):
    assert Settings(api_prefix=raw).api_prefix == expected


def test_api_prefix_rejects_windows_path():
    """Git Bash / MSYS 会把 `/xxx` 自动转成 Windows 绝对路径，必须拦截。"""
    mangled = r"C:/Program Files/Git/voicehub-api"
    with pytest.raises(ValidationError) as exc:
        Settings(api_prefix=mangled)
    message = str(exc.value)
    assert "MSYS_NO_PATHCONV" in message
    assert ".env" in message


def test_api_prefix_rejects_backslash_path():
    with pytest.raises(ValidationError):
        Settings(api_prefix=r"C:\voicehub\api")


def test_default_api_prefix_is_api():
    """默认前缀保持 /api，保证既有部署与测试不受影响。"""
    assert Settings().api_prefix == "/api"


def test_frontend_path_defaults_to_project_frontend():
    """管理后台静态目录默认指向项目根目录下的 frontend/。"""
    path = Settings().frontend_path
    assert path.name == "frontend"
    assert path.parent.name == "嘉环声枢录音卡业务系统"


def test_test_suite_pins_api_prefix():
    """测试环境固定 /api 前缀。

    conftest 在导入应用前写入 ``API_PREFIX=/api``，因此本机 ``backend/.env``
    里配置的生产前缀（如 /voicehub-api）不会让用例里写死的路径全部失效。
    """
    from app.core.config import settings

    assert settings.api_prefix == "/api"


def test_casdoor_client_id_is_configured():
    """Casdoor clientId 已内置（公开标识符），clientSecret 仍须由环境提供。"""
    from app.core.config import settings

    assert settings.casdoor_client_id == "6932b8ce0b655b9421b5"
    # 未配置 secret 时不得声称具备 client 凭据能力
    assert settings.casdoor_has_client_credentials is (
        bool(settings.casdoor_client_id and settings.casdoor_client_secret)
    )
