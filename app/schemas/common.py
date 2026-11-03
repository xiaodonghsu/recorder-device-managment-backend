"""通用响应结构。"""

from __future__ import annotations

from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageQuery(BaseModel):
    """分页查询基类。"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=200, description="每页条数，最大 200")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class PageResult(BaseModel, Generic[T]):
    """分页结果。"""

    items: List[T]
    total: int = Field(description="总记录数")
    page: int
    page_size: int

    @classmethod
    def build(cls, items: List[T], total: int, query: PageQuery) -> "PageResult[T]":
        return cls(items=items, total=total, page=query.page, page_size=query.page_size)


class MessageOut(BaseModel):
    """通用操作结果。"""

    success: bool = True
    message: str = "操作成功"


class ErrorOut(BaseModel):
    """统一错误响应。"""

    code: str = Field(description="业务错误码")
    message: str = Field(description="错误描述")
    status: int = Field(description="HTTP 状态码")
    details: Optional[dict] = Field(default=None, description="错误明细")
