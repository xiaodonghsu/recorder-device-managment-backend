"""pytest 公共夹具。

测试使用 SQLite 内存库（StaticPool 保持连接共享），业务逻辑与 MySQL 生产环境
共用同一套 ORM 与服务层代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试固定使用 /api 前缀。
# 必须在导入 app.main（进而导入 app.core.config）之前设置：
# pydantic-settings 的优先级是 环境变量 > .env 文件 > 默认值，
# 这样本机 backend/.env 里的 API_PREFIX 不会影响测试用例里写死的路径。
os.environ["API_PREFIX"] = "/api"

from app.api.deps import get_current_user  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.integrations.casdoor import CasdoorIdentity  # noqa: E402
from app.main import app  # noqa: E402

TENANT = "acme"
OTHER_TENANT = "globex"


def make_identity(
    user_id: str,
    *,
    tenant_id: str = TENANT,
    username: str = "tester",
    is_admin: bool = False,
    is_global_admin: bool = False,
    roles=None,
    groups=None,
    permissions=None,
) -> CasdoorIdentity:
    return CasdoorIdentity(
        user_id=user_id,
        username=username,
        display_name=username,
        tenant_id=tenant_id,
        is_admin=is_admin,
        is_global_admin=is_global_admin,
        roles=list(roles or []),
        groups=list(groups or []),
        permissions=list(permissions or []),
        auth_method="test",
    )


@pytest.fixture()
def engine() -> Generator[Engine, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(test_engine, "connect")
    def _fk_on(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture()
def session(engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """TestClient，数据库依赖指向内存库，认证依赖由用例自行覆盖。"""
    import app.main as main_module

    monkeypatch.setattr(main_module, "init_db", lambda *a, **k: None)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth(client: TestClient):
    """认证覆盖工厂：把当前请求身份替换为指定用户。"""

    def _apply(
        user_id: str = "u-admin",
        *,
        tenant_id: str = TENANT,
        is_admin: bool = True,
        is_global_admin: bool = False,
        username: str = "tester",
        roles=None,
        groups=None,
        permissions=None,
    ) -> None:
        from app.api.deps import CurrentUser

        identity = make_identity(
            user_id,
            tenant_id=tenant_id,
            username=username,
            is_admin=is_admin,
            is_global_admin=is_global_admin,
            roles=roles,
            groups=groups,
            permissions=permissions,
        )
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(identity=identity)

    return _apply
