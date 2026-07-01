# ADR-0006 — Одноразовая миграция данных SQLite → PostgreSQL

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-01 |
| Связано | ADR-0001, ADR-0003, ADR-0005; [04-data-model.md](../04-data-model.md) §«Маппинг», [07-deployment.md](../07-deployment.md) |

## Context

При переходе на PostgreSQL ([ADR-0001](./ADR-0001-postgres-sqlalchemy-async.md)) существующие данные в `data/service.db` (SQLite) нужно перенести без потери истории. Модель меняется:

- `projects` → `teams` (появляется `leader_user_id`);
- `telegram_users` (подписчики через `/start`) → `users` (аккаунты) + `telegram_links` (привязки);
- `user_project_access` (M:N) → `users.team_id` (single-team, [ADR-0003](./ADR-0003-roles-and-teams.md));
- `twilio_numbers` → `phone_numbers` (`team_id`, `added_by_user_id`);
- `inbound_messages` → `inbound_sms` (`team_id`, `raw_payload` JSONB);
- `telegram_deliveries` → `deliveries` (через маппинг user_id + снимок chat_id).

Auth-модели раньше не было (Basic/token), поэтому у legacy-пользователей нет паролей.

## Decision

Одноразовый идемпотентный скрипт `scripts/migrate_sqlite_to_pg.py`, запускается **после** `alembic upgrade head`:

```
python -m scripts.migrate_sqlite_to_pg --sqlite <path> --database-url <url> --orphan-team-name Legacy
```

Порядок (с сохранением исходных id для совпадения FK; в конце — `setval` sequences; вставки `ON CONFLICT DO NOTHING`):

1. **`projects` → `teams`** (`leader_user_id` проставляется позже; `description` не переносится — `Q-DATA-1`).
2. **`telegram_users` → `users` + `telegram_links`.** `users.username = 'tg_'+telegram_id`, `password_hash=NULL`, `role='group_member'`. `team_id` из `user_project_access`: единственный проект → его команда; несколько → первый (лишние теряются, фиксируются в отчёте — `TD-004`); ноль → служебная команда `--orphan-team-name` (`Legacy`). `telegram_links(telegram_user_id=telegram_id, user_id=new_id, dead_at=now() если был неактивен)`. Держать маппинг `old telegram_users.id → new user_id`.
3. **Лидеры.** Для каждой команды `leader = min(user_id)` среди её участников → ему `role='group_leader'`, `teams.leader_user_id = leader`.
4. **`twilio_numbers` → `phone_numbers`** (`team_id ← project_id`, `added_by_user_id ← leader`).
5. **`inbound_messages` → `inbound_sms`** (`team_id ← project_id`, `raw_payload_json::jsonb → raw_payload`; `twilio_message_sid` сохраняется — partial-UNIQUE).
6. **`telegram_deliveries` → `deliveries`** (через маппинг `telegram_user_id(FK на telegram_users.id) → deliveries.user_id` + снимок chat_id в `deliveries.telegram_user_id`; UNIQUE dedup `(inbound_sms_id, telegram_user_id)`).
7. **`service_state`** 1:1, кроме `telegram_offset` (long polling удалён, [ADR-0005](./ADR-0005-sms-addressing-via-team.md)).

Скрипт печатает отчёт: число строк по таблицам + списки осиротевших и мульти-проектных пользователей.

## Rationale

- **Порядок вставки** следует зависимостям FK; циклический FK team↔user разрешается двухфазно (сначала users без team или с team, лидер — отдельным UPDATE), опираясь на DEFERRABLE `users.team_id`.
- **Сохранение id** позволяет переносить дочерние строки без перестроения ссылок; `setval` в конце восстанавливает автоинкремент.
- **Идемпотентность** (`ON CONFLICT DO NOTHING`) делает повторный прогон безопасным (например, после сбоя) — не создаёт дублей.
- **single-team компромисс:** первый проект как команда — детерминированное правило; редкие мульти-проектные случаи явно репортятся для ручной донастройки (`TD-004`).
- **Служебная команда `Legacy`** сохраняет пользователей без проектов, соблюдая инвариант «member ⇒ team_id NOT NULL».

## Consequences

**Плюсы:** история (SMS/доставки) сохраняется; повторяемый, наблюдаемый (отчёт) прогон; совместим с инвариантами новой схемы.

**Минусы / издержки:**
- Legacy-пользователи получают синтетический `username='tg_<id>'` и `password_hash=NULL` — веб-вход возможен только после назначения им нормального логина/пароля админом (или они используют Mini App SSO по существующей привязке).
- Мульти-проектные доступы усекаются до одной команды (`TD-004`).
- `projects.description` теряется (`Q-DATA-1`).
- Требует ручной проверки инвариантов после прогона (см. [06-testing-strategy.md](../06-testing-strategy.md) §20).

## Alternatives

1. **Начать с чистой БД (не мигрировать).** Отклонено: теряется история SMS/доставок и текущие подписчики/номера.
2. **Онлайн dual-write в обе БД.** Отклонено: избыточно сложно для разовой смены хранилища; сервис можно кратко остановить.
3. **Переносить `user_project_access` как M:N.** Отклонено: противоречит single-team-решению [ADR-0003](./ADR-0003-roles-and-teams.md); модель усложнилась бы ради legacy-данных.
4. **Автоматически заводить пароли legacy-пользователям.** Отклонено: небезопасно (нет канала доставки пароля); `password_hash=NULL` + первый вход/Mini App SSO корректнее.
