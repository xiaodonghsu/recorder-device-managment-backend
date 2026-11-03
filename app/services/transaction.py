"""通用事务封装。

FastAPI 的会话依赖在请求结束时统一提交/回滚；此处提供 SAVEPOINT 级别的
局部原子性，保证单个业务操作（入库 / 领用 / 归还 / 绑定 / 解绑）
要么全部成功，要么全部不生效（README §4 实现注意 3）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session


@contextmanager
def transaction(db: Session) -> Iterator[Session]:
    """开启一个局部事务块。

    使用 SAVEPOINT（MySQL / SQLite 均支持），可安全嵌套在外层读事务中。
    块内抛出异常时自动回滚到保存点，数据库状态回到进入块前。
    """
    nested = db.begin_nested()
    try:
        yield db
    except Exception:
        nested.rollback()
        raise
    else:
        nested.commit()
