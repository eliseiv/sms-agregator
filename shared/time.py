"""Единые time-хелперы (перенесены из app/infrastructure/db.py при уходе от SQLite)."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Текущее время в UTC (timezone-aware)."""
    return datetime.now(UTC)


def iso_now() -> str:
    """ISO-8601 представление текущего времени в UTC."""
    return utc_now().isoformat()
