"""FastAPI-приложение: lifespan, middleware, роутеры (docs/03-architecture)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.csrf import CSRFMiddleware
from app.api.middlewares import (
    MethodOverrideMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from app.api.routers.admin import router as admin_router
from app.api.routers.admin_numbers import router as admin_numbers_router
from app.api.routers.admin_ui import router as admin_ui_router
from app.api.routers.auth import router as auth_router
from app.api.routers.landing import router as landing_router
from app.api.routers.numbers import router as numbers_router
from app.api.routers.telegram_auth import router as telegram_auth_router
from app.api.routers.telegram_webhook import router as telegram_webhook_router
from app.api.routers.webhooks import router as webhook_router
from app.api.schemas import HealthResponse
from app.application.auth_service import seed_admin
from app.application.workers import delivery_retry_loop
from app.exceptions import install_exception_handlers
from shared.config import get_settings
from shared.db import dispose_engine, init_engine, make_session
from shared.logging import configure_logging, get_logger
from shared.redis_client import close_redis

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "api" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, service="api")
    init_engine("api")

    # Seed super_admin (идемпотентно).
    async with make_session() as session:
        async with session.begin():
            await seed_admin(session)

    stop_event = asyncio.Event()
    app.state.stop_event = stop_event
    tasks = [asyncio.create_task(delivery_retry_loop(settings, stop_event))]
    app.state.background_tasks = tasks
    logger.info("service_started", service=settings.SERVICE_NAME)
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispose_engine()
        await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SMS Aggregator API",
        description="Приём SMS из Twilio и доставка в Telegram с командами и Mini App SSO.",
        version="2.0.0",
        lifespan=lifespan,
    )

    install_exception_handlers(app)

    # Middleware: добавляем в обратном порядке → итоговая цепочка
    # CSRF → MethodOverride → Session → SecurityHeaders → RequestID.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(MethodOverrideMiddleware)
    app.add_middleware(CSRFMiddleware)

    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR), check_dir=False),
        name="static",
    )

    app.include_router(webhook_router)
    app.include_router(auth_router)
    app.include_router(telegram_auth_router)
    app.include_router(telegram_webhook_router)
    app.include_router(admin_router)
    app.include_router(admin_numbers_router)
    app.include_router(admin_ui_router)
    app.include_router(numbers_router)
    app.include_router(landing_router)

    @app.get("/health", response_model=HealthResponse, tags=["System"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service=get_settings().SERVICE_NAME)

    return app


app = create_app()
