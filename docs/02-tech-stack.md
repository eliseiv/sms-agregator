# 02. Tech Stack

Стек фиксируется здесь и только здесь. Другие агенты language-agnostic — берут язык, инструменты и команды из этого файла. Если что-то не зафиксировано — поднять `Q-NNN-N`, не угадывать.

Источник решения — [ADR-0001](./adr/ADR-0001-postgres-sqlalchemy-async.md).

## Язык и рантайм

| Компонент | Выбор | Версия | Обоснование |
| --- | --- | --- | --- |
| Язык | Python | 3.12 | Текущий код на Python; референс mail-agregator на 3.12; async-стек зрелый. |
| Web-framework | FastAPI | >=0.110,<1.0 | Уже используется; async-first; Depends/lifespan; OpenAPI. |
| ASGI-сервер | uvicorn | >=0.29 | Стандарт для FastAPI. |

## Данные и хранилище

| Компонент | Выбор | Версия | Обоснование |
| --- | --- | --- | --- |
| СУБД | PostgreSQL | 16 | JSONB, partial indexes, DEFERRABLE FK, триггеры — всё нужно (см. 04). Заменяет SQLite. |
| ORM | SQLAlchemy | 2.0.x (async) | `DeclarativeBase`, async engine, типизированные модели; порт паттернов mail-agregator. |
| DB-драйвер | asyncpg | >=0.29 | Быстрый async-драйвер PostgreSQL (`postgresql+asyncpg://`). |
| Миграции | Alembic | >=1.13 | Версионирование схемы, autogenerate, обратимые миграции. |
| Кэш/сессии | Redis | 7 | Сессии, setup-сессии, pending-токены Mini App SSO, rate-limit, lockout-счётчики. |
| Redis-клиент | redis (redis-py) | >=5.0 (async) | `redis.asyncio`. |

## Безопасность

| Компонент | Выбор | Версия | Обоснование |
| --- | --- | --- | --- |
| Хеширование паролей | argon2-cffi | >=23.1 | argon2id — современный memory-hard KDF (см. [08-security.md](./08-security.md)). |
| Проверка подписи Twilio | twilio (RequestValidator) | >=9.0 | Официальный SDK; валидация `X-Twilio-Signature`. |

## Интеграции / прочее

| Компонент | Выбор | Версия | Обоснование |
| --- | --- | --- | --- |
| HTTP-клиент | httpx | >=0.27 | Async-вызовы Telegram Bot API (с поддержкой proxy). |
| Шаблоны | Jinja2 | >=3.1 | SSR админ-панели и страниц логина. |
| Формы/аплоады | python-multipart | >=0.0.9 | Разбор form-urlencoded (webhook Twilio, POST-формы логина). |
| Конфиг | pydantic-settings | >=2.2 | Типизированный `Settings` из env (`shared/config.py`). |
| Часовые пояса | zoneinfo (stdlib) | — | Форматирование времени SMS в `TIMEZONE`. |

## Структура пакетов

- `shared/` — переносимый слой инфраструктуры: `config.py` (pydantic-settings), `db.py` (Base, async engine, сессии), `models/` (SQLAlchemy-модели всех таблиц).
- `migrations/` — Alembic (`env.py` async, `versions/`).
- `app/` — DDD-слои приложения: `api/` (роутеры, middleware, шаблоны, static, deps), `application/` (сервисы: sms-пайплайн, auth, admin, teams, telegram SSO), `domain/` (entities, протоколы репозиториев), `infrastructure/` (репозитории SQLAlchemy, Redis-клиент, telegram_api, twilio_security, sessions, rate_limit), `core/` (security/argon2), `telegram/` (`init_data.py` — HMAC).
- `scripts/` — one-off (`migrate_sqlite_to_pg.py`).

Подробнее — [03-architecture.md](./03-architecture.md).

## Команды (обязательны для исполнителей)

Проект использует единый инструментарий качества:

| Действие | Команда |
| --- | --- |
| Форматирование | `ruff format .` |
| Lint | `ruff check .` |
| Type-check | `mypy shared app scripts` |
| Тесты | `pytest` (см. [06-testing-strategy.md](./06-testing-strategy.md)) |
| Миграции (применить) | `alembic upgrade head` |
| Миграции (autogenerate) | `alembic revision --autogenerate -m "<msg>"` |

Инструменты dev-зависимостей: `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `httpx` (тест-клиент), `asgi-lifespan` (по необходимости).

> `Q-TECH-1` (см. [99-open-questions.md](./99-open-questions.md)): формат манифеста зависимостей — `requirements.txt` (текущий) vs `pyproject.toml` (референс mail-agregator). До ответа backend/devops сохраняют существующий `requirements.txt` и добавляют туда новые зависимости.

## Совместимость версий

- SQLAlchemy 2.0 async + asyncpg требует URL вида `postgresql+asyncpg://user:pass@host:5432/db`.
- Redis-py 5.x — `redis.asyncio.Redis`.
- Все datetime — `TIMESTAMPTZ` в UTC; форматирование в локальную зону только на уровне представления.
