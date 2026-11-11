"""数据库会话与引擎管理。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_sqlite_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return
    # sqlite:///./data/dev.db 或 sqlite:///C:/.../data/dev.db
    raw = database_url.split("sqlite:///", 1)[-1]
    if not raw or raw == ":memory:":
        return
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_for_url(database_url: str, *, echo: bool | None = None) -> Engine:
    """按数据库 URL 创建引擎。

    SQLite 与 MySQL 的连接池参数不同，此处统一处理。
    """
    is_sqlite = database_url.startswith("sqlite")
    kwargs: dict = {
        "echo": settings.db_echo if echo is None else echo,
        "future": True,
        "hide_parameters": True,
    }

    if is_sqlite:
        _ensure_sqlite_dir(database_url)
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )

    engine = create_engine(database_url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            # SQLite 默认不强制外键，需要显式开启，才能让组合外键等约束真正生效
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine_for_url(settings.database_url)
        logger.info("数据库引擎已创建: %s", settings.database_url.split("@")[-1])
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供请求级数据库会话。

    正常返回时提交事务；请求过程中抛出异常时回滚，保证「一次请求 = 一个事务」。
    单个业务操作内部的原子性由 services.transaction.transaction（SAVEPOINT）保证。
    """
    db = get_sessionmaker()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    finally:
        db.close()


def init_db(engine: Engine | None = None, *, drop_first: bool = False) -> None:
    """建表并升级旧版凭证表（可选先删表）。"""
    target = engine or get_engine()
    if drop_first:
        logger.warning("即将删除所有表并重建（drop_first=True）")
        Base.metadata.drop_all(target)
    Base.metadata.create_all(target)
    _upgrade_credential_table(target)
    logger.info("数据库表结构已就绪")


def _upgrade_credential_table(engine: Engine) -> None:
    """迁移旧凭证表，保留版本记录并为无法还原的明文留空。"""
    with engine.begin() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("device_credential")}
        if "auth_key_hash" not in columns:
            return

        if "auth_key" not in columns:
            conn.exec_driver_sql("ALTER TABLE device_credential ADD COLUMN auth_key VARCHAR(128)")
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("DROP INDEX IF EXISTS idx_credential_hash")
        conn.exec_driver_sql("ALTER TABLE device_credential DROP COLUMN auth_key_hash")
        logger.info("已升级设备凭证表，旧凭证的 AuthKey 需要补录或轮换")


def reset_engine() -> None:
    """测试用：重置全局引擎与会话工厂。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
