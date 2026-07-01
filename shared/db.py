"""Async SQLAlchemy 2.x engine + session factory (docs/03-architecture §Доступ к БД).

Две роли пула: ``api`` (pool_size=10, max_overflow=20), ``worker`` (5/5).
Подключение через asyncpg. ``get_session`` — FastAPI dependency; ``make_session`` —
контекстный менеджер для фоновых задач/скриптов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from shared.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base для всех ORM-моделей в :mod:`shared.models`."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: Settings, role: Literal["api", "worker"]) -> AsyncEngine:
    pool_size = 5 if role == "worker" else 10
    max_overflow = 5 if role == "worker" else 20
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
        echo=False,  # никогда не echo — утекли бы параметры в логи
    )


def init_engine(role: Literal["api", "worker"] = "api") -> AsyncEngine:
    """Собрать глобальный engine + session factory. Вызывается один раз при старте."""
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine(get_settings(), role)
        _session_factory = async_sessionmaker(
            bind=_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def dispose_engine() -> None:
    """Освободить engine — shutdown-хук и тесты."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yield-ит сессию и закрывает её на выходе.

    Без неявной транзакции — call-site открывает ``async with session.begin():``
    для write-транзакций.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def make_session() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер для не-FastAPI вызовов (worker-задачи, скрипты)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()
