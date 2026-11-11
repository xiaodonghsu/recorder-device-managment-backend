"""FastAPI 应用入口。

启动流程：
1. 校验配置（生产环境风险项）；
2. 初始化数据库连接池，开发环境自动建表；
3. 创建共享 httpx 客户端与 Casdoor 客户端；
4. 注册路由、异常处理器、CORS。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, bindings, credentials, devices, me, tables
from app.core.config import settings
from app.core.errors import AppError
from app.db.session import get_engine, init_db
from app.integrations.casdoor import CasdoorClient

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    warnings = settings.validate_runtime()
    for item in warnings:
        logger.warning("配置风险提示：%s", item)

    if settings.app_env.lower() != "production":
        init_db(get_engine())
    else:
        logger.info("生产环境跳过自动建表，请使用数据库初始化脚本")

    http_client = httpx.AsyncClient(
        timeout=settings.casdoor_timeout,
        verify=settings.casdoor_verify_tls,
        follow_redirects=True,
    )
    app.state.http_client = http_client
    app.state.casdoor_client = CasdoorClient(settings, http_client)
    logger.info("Casdoor 客户端已就绪：%s", settings.casdoor_base_url)

    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "嘉环声枢录音卡设备管理后端。\n\n"
        "**核心模型**：Device（设备属于企业）→ Assignment（分配给员工）→ "
        "Binding（员工通过 APP 获得操作权限）→ Nickname（员工自定义昵称）→ "
        "Credential（设备独立凭证）→ Event（完整生命周期）。\n\n"
        "所有员工接口通过 Casdoor Access Token 认证；管理员接口额外校验企业与角色权限。"
    ),
    lifespan=lifespan,
    docs_url="/voicehub-api/docs",
    redoc_url="/voicehub-api/redoc",
    openapi_url="/voicehub-api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "参数校验失败",
            "status": 422,
            "details": {"errors": exc.errors()},
        },
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
for module in (auth, devices, bindings, credentials, me, tables):
    app.include_router(module.router, prefix=settings.api_prefix)

@app.get("/healthz", tags=["运维"], summary="存活探针")
async def healthz() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/readyz", tags=["运维"], summary="就绪探针")
async def readyz() -> JSONResponse:
    from sqlalchemy import text

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        logger.error("数据库不可用：%s", exc)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    return JSONResponse(status_code=200, content={"status": "ready", "database": "up"})


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)
