"""FastAPI application factory. Run: uvicorn --factory aster_api.main:create_app"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from aster_api import __version__
from aster_api.admin.routes import router as admin_router
from aster_api.age.routes import router as age_router
from aster_api.auth.routes import router as auth_router
from aster_api.characters.routes import router as characters_router
from aster_api.conversations.routes import router as conversations_router
from aster_api.core.config import Settings, get_settings
from aster_api.core.context import AppContext
from aster_api.core.errors import install_error_handlers
from aster_api.core.logging import configure_logging
from aster_api.ledger.routes import router as credits_router

log = logging.getLogger("aster.http")


class Health(BaseModel):
    status: str
    version: str
    env: str


class Ready(BaseModel):
    status: str
    database: bool
    redis: bool


def create_app(settings: Settings | None = None, *, manage_logging: bool = True) -> FastAPI:
    settings = settings or get_settings()
    if manage_logging:
        configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ctx = await AppContext.create(settings)
        try:
            yield
        finally:
            await app.state.ctx.close()

    app = FastAPI(
        title="Aster API",
        version=__version__,
        description="Aster v1 API. Chat routes require a server-verified 18+ age attestation.",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.is_dev_like else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.is_dev_like else None,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = str(uuid.uuid4())
        request.state.request_id = rid
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        route = request.scope.get("route")
        log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),  # template, never the raw path/query
                "status": response.status_code,
                "ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return response

    @app.get("/health", response_model=Health, tags=["ops"])
    async def health() -> Health:
        return Health(status="ok", version=__version__, env=settings.app_env)

    @app.get("/ready", response_model=Ready, tags=["ops"])
    async def ready(request: Request, response: Response) -> Ready:
        ctx: AppContext = request.app.state.ctx
        db_ok = redis_ok = False
        try:
            db_ok = await ctx.db.ping()
        except Exception:
            log.warning("db not ready")
        try:
            redis_ok = bool(await ctx.redis.ping())
        except Exception:
            log.warning("redis not ready")
        if not (db_ok and redis_ok):
            response.status_code = 503
        return Ready(status="ok" if db_ok and redis_ok else "degraded", database=db_ok, redis=redis_ok)

    for router in (auth_router, age_router, characters_router, conversations_router, credits_router, admin_router):
        app.include_router(router)
    return app
