# ADR-0001 — Переход на PostgreSQL + SQLAlchemy 2.0 async + Alembic + Redis

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-01 |
| Связано | ADR-0002, ADR-0004, ADR-0006; [02-tech-stack.md](../02-tech-stack.md), [04-data-model.md](../04-data-model.md) |

## Context

Текущий сервис использует **синхронный `sqlite3`** (raw SQL, глобальный `db` в `app/infrastructure/db.py`) и не имеет отдельного хранилища для сессий/rate-limit. Предстоящая доработка вводит: админ-панель с сессиями, двухэтапный логин, argon2, Telegram Mini App SSO с pending-токенами, rate-limit и lockout, а также многопользовательскую модель с командами. SQLite и синхронный доступ этому мешают:

- нет типов JSONB, partial-индексов, DEFERRABLE FK, серверных триггеров — всё это нужно для целевой схемы ([04-data-model.md](../04-data-model.md));
- нет места для эфемерного состояния (сессии, pending-токены, rate-limit-счётчики);
- синхронный доступ конфликтует с async-webhook/доставкой и не масштабируется на конкурентные запросы;
- существует уже реализованный референс того же класса (mail-agregator) на PostgreSQL + SQLAlchemy 2.0 async + Alembic + Redis + argon2 — переиспользование паттернов резко снижает риск.

## Decision

Заменить SQLite на следующий стек (фиксируется в [02-tech-stack.md](../02-tech-stack.md)):

- **PostgreSQL 16** — основная БД.
- **SQLAlchemy 2.0 (async)** + **asyncpg** — весь data-слой становится async; declarative-модели в `shared/models/`.
- **Alembic** — версионирование схемы (async `env.py`, обратимые миграции, `compare_type=True`).
- **Redis 7** — сессии, setup-сессии, pending-токены Mini App SSO, rate-limit, lockout-счётчики.
- **argon2-cffi** — хеширование паролей (детали — [08-security.md](../08-security.md)).

Структурно вводится переносимый пакет `shared/` (`config.py`, `db.py`, `models/`) и `migrations/`, по образцу mail-agregator, чтобы код был переносим. DDD-слои `app/` сохраняются; репозитории переписываются на `AsyncSession`. Доставка SMS выполняется фоновым loop внутри процесса `app` (отдельный worker-процесс не вводится — `TD-002`).

Пулы соединений по роли engine: `api` = pool_size 10 / max_overflow 20; `worker` = 5/5; `pool_pre_ping=True`. Миграции применяются отдельным compose-сервисом `migrate` (`alembic upgrade head`), не в lifespan приложения.

## Rationale

- Целевые фичи прямо требуют возможностей PostgreSQL (JSONB, partial-UNIQUE, DEFERRABLE FK, триггеры) и Redis (сессии/лимиты) — SQLite их не даёт.
- Async-стек соответствует уже async webhook/доставке и снимает блокировки при конкурентных запросах.
- Наличие проверенного референса (mail-agregator) позволяет переносить auth/SSO/admin 1:1, минимизируя проектный риск.
- Простота сохранена: один монолит + один фоновый loop; без микросервисов и очередей сверх Redis.

## Consequences

**Плюсы:** промышленная СУБД, серверные инварианты в схеме, единое место для сессий/лимитов, переносимость кода, async сквозной.

**Минусы / издержки:**
- Нужны PostgreSQL и Redis в docker-compose (см. [07-deployment.md](../07-deployment.md)) — рост инфраструктуры.
- Требуется одноразовая миграция данных SQLite → PostgreSQL ([ADR-0006](./ADR-0006-data-migration-sqlite-to-pg.md)).
- Весь data-слой (репозитории) переписывается на async SQLAlchemy; удаляются `Sqlite*Repository` и глобальный `db`.

## Alternatives

1. **Остаться на SQLite + добавить отдельные компоненты для сессий.** Отклонено: нет JSONB/partial-UNIQUE/DEFERRABLE FK/триггеров, слабая конкурентность, всё равно нужен Redis; разнородный стек сложнее референса.
2. **PostgreSQL + синхронный SQLAlchemy.** Отклонено: конфликт с async webhook/доставкой, расхождение с референсом, отсутствие выигрыша в простоте.
3. **Другой async ORM (Tortoise/SQLModel) или raw asyncpg.** Отклонено: референс и экспертиза на SQLAlchemy 2.0; Alembic-миграции зрелые; raw SQL повышает риск ошибок в сложной схеме с инвариантами.
4. **MySQL/MariaDB.** Отклонено: слабее по partial-индексам/JSONB/DEFERRABLE FK; референс на PostgreSQL.
