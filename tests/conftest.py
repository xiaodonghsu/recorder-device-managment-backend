"""pytest 公共夹具。

测试使用 SQLite 内存库（StaticPool 保持连接共享），业务逻辑与 MySQL 生产环境
共用同一套 ORM 与服务层代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator, Optional

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
from app.integrations.casdoor import (  # noqa: E402
    CasdoorIdentity,
    EmployeeLookup,
    get_casdoor_client,
)
from app.main import app  # noqa: E402

TENANT = "acme"
OTHER_TENANT = "globex"


class FakeCasdoorClient:
    """Casdoor 查询替身：默认「工号存在」，让所有既有测试保持原有行为。

    单元测试不应真的访问 production Casdoor；需要模拟
    「不存在 / 跨组织 / 禁用 / 查询不可用」时，用 ``lookup_result`` 覆盖。
    """

    def __init__(self) -> None:
        from app.core.config import settings

        self.cfg = settings
        self.lookup_result: Optional[EmployeeLookup] = None
        self.user_object: Optional[dict] = None
        self.lookup_calls: list = []
        self.user_calls: list = []

    async def lookup_employee(self, owner, employee_no, *, access_token=None):
        self.lookup_calls.append((owner, employee_no, access_token))
        if self.lookup_result is not None:
            return self.lookup_result
        return EmployeeLookup(
            "found",
            employee_no=employee_no,
            display_name="测试员工",
            owner=owner,
            user_id=f"uuid-{employee_no}",
        )

    async def get_user(self, owner, user_id_or_name, *, access_token=None):
        self.user_calls.append((owner, user_id_or_name))
        return self.user_object


def make_identity(
    user_id: str,
    *,
    tenant_id: str = TENANT,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    is_admin: bool = False,
    is_global_admin: bool = False,
    roles=None,
    groups=None,
    permissions=None,
) -> CasdoorIdentity:
    """构造测试身份。

    ``username`` 语义是工号（preferred_username），默认等于 user_id，
    避免所有测试用户共用一个工号；``display_name`` 是姓名，仅用于展示。
    """
    employee_no = username if username is not None else user_id
    return CasdoorIdentity(
        user_id=user_id,
        username=employee_no,
        display_name=display_name if display_name is not None else employee_no,
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
def casdoor() -> Generator[FakeCasdoorClient, None, None]:
    """把 /devices/{id}/assign 的 Casdoor 依赖替换为可控替身。"""
    fake = FakeCasdoorClient()
    app.dependency_overrides[get_casdoor_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_casdoor_client, None)


@pytest.fixture()
def client(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, casdoor: FakeCasdoorClient
) -> Generator[TestClient, None, None]:
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
        username: Optional[str] = None,
        display_name: Optional[str] = None,
        roles=None,
        groups=None,
        permissions=None,
        access_token: Optional[str] = None,
    ) -> None:
        from app.api.deps import CurrentUser

        identity = make_identity(
            user_id,
            tenant_id=tenant_id,
            username=username,
            display_name=display_name,
            is_admin=is_admin,
            is_global_admin=is_global_admin,
            roles=roles,
            groups=groups,
            permissions=permissions,
        )
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            identity=identity, access_token=access_token
        )

    return _apply
