# 04. Data Model

СУБД — **PostgreSQL 16**. Кодировка UTF-8. Все временные поля — `TIMESTAMPTZ` в UTC. Все PK — `BIGINT` (`BIGSERIAL`/identity). JSONB для сырых payload'ов.

Источники решений: [ADR-0001](./adr/ADR-0001-postgres-sqlalchemy-async.md) (стек/схема), [ADR-0003](./adr/ADR-0003-roles-and-teams.md) (роли/команды), [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md) (`telegram_links`), [ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md) (адресация), [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md) (миграция), [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md) (unassigned-номера: `phone_numbers.team_id` NULLABLE + `ON DELETE SET NULL`), [ADR-0012](./adr/ADR-0012-multi-team-membership.md) (multi-team: аддитивная M:N `user_teams`, `users.team_id` = домашняя команда).

Общие конвенции: `created_at`/`updated_at` — `TIMESTAMPTZ NOT NULL DEFAULT now()`; триггер `set_updated_at()` (BEFORE UPDATE) обновляет `updated_at`; частичные индексы по `is_active`/`dead_at`.

## ER-диаграмма

```mermaid
erDiagram
    teams  ||--o| users : "leader (1:1 via teams.leader_user_id)"
    teams  ||--o{ users : "home team (1:N via users.team_id)"
    users  ||--o{ user_teams : "membership (M:N)"
    teams  ||--o{ user_teams : "membership (M:N)"
    teams  ||--o{ phone_numbers : "owns (nullable = unassigned)"
    teams  ||--o{ inbound_sms : "addressed_to (nullable)"
    users  ||--o{ telegram_links : "linked (1:N)"
    users  ||--o{ deliveries : "recipient"
    users  ||--o{ phone_numbers : "added_by (nullable)"
    inbound_sms ||--o{ deliveries : "delivered_as"

    teams {
        bigint id PK
        text name UK "1..100"
        bigint leader_user_id FK "UNIQUE; NULL; ON DELETE RESTRICT"
        boolean is_active
        timestamptz created_at
        timestamptz updated_at
    }
    users {
        bigint id PK
        text username UK "lower-case"
        varchar password_hash "VARCHAR(255), nullable, argon2id"
        text role "super_admin|group_leader|group_member"
        bigint team_id FK "nullable; DEFERRABLE; ON DELETE SET NULL"
        text display_name "nullable"
        boolean password_reset_required
        timestamptz lockout_until "nullable"
        int failed_login_attempts
        timestamptz last_login_at "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    user_teams {
        bigint id PK
        bigint user_id FK "ON DELETE CASCADE"
        bigint team_id FK "ON DELETE CASCADE"
        timestamptz created_at
    }
    telegram_links {
        bigint telegram_user_id PK "= chat_id"
        bigint user_id FK "indexed, NOT unique; ON DELETE CASCADE"
        timestamptz created_at
        timestamptz dead_at "nullable"
    }
    phone_numbers {
        bigint id PK
        text phone_number UK "E.164"
        bigint team_id FK "nullable (NULL=unassigned); ON DELETE SET NULL"
        bigint added_by_user_id FK "nullable; ON DELETE SET NULL"
        text label "nullable"
        boolean is_active
        timestamptz created_at
        timestamptz updated_at
    }
    inbound_sms {
        bigint id PK
        text twilio_message_sid "nullable; partial-UNIQUE"
        text from_number
        text to_number
        text body
        bigint team_id FK "nullable; ON DELETE SET NULL"
        jsonb raw_payload
        timestamptz received_at
        timestamptz created_at
    }
    deliveries {
        bigint id PK
        bigint inbound_sms_id FK "ON DELETE CASCADE"
        bigint user_id FK "ON DELETE CASCADE"
        bigint telegram_user_id "снимок chat_id, без FK"
        text status "pending|sent|failed|dead"
        int attempts
        text last_error "nullable"
        timestamptz sent_at "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    admin_audit {
        bigint id PK
        bigint actor_user_id "без FK"
        text action
        bigint target_user_id "nullable"
        text target_username "nullable snapshot"
        jsonb details "nullable"
        text ip "nullable"
        text user_agent "nullable"
        timestamptz created_at
    }
    service_state {
        text key PK
        text value
        timestamptz updated_at
    }
```

---

## Таблицы

### `teams` (было: `projects`)

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `name` | TEXT | NOT NULL, UNIQUE, CHECK length 1..100 | Имя команды. |
| `leader_user_id` | BIGINT | NULL, UNIQUE, FK → `users(id)` ON DELETE RESTRICT | Лидер команды. NULL допустим только для «orphan»-команды без участников (сразу после создания, до добавления первого участника). UNIQUE → один user лидирует максимум одной командой. RESTRICT → нельзя удалить пользователя, пока он лидер (сначала переназначить/удалить команду). |
| `is_active` | BOOLEAN | NOT NULL DEFAULT true | |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Триггер `set_updated_at()`. |

**Индексы:** UNIQUE(`name`); UNIQUE(`leader_user_id`) (частичный `WHERE leader_user_id IS NOT NULL`).

**Правило «первый=лидер»:** при добавлении первого участника в команду с `leader_user_id IS NULL` — этот участник получает `role='group_leader'` и записывается в `teams.leader_user_id` (см. [ADR-0003](./adr/ADR-0003-roles-and-teams.md), реализация в `teams_service.set_leader_if_absent`).

---

### `users`

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `username` | TEXT | NOT NULL, UNIQUE, CHECK `username = lower(username)` | Логин. Нормализуется в lower-case приложением; CHECK — defense-in-depth. Для legacy tg-аккаунтов после миграции — `'tg_'+telegram_id`. |
| `password_hash` | VARCHAR(255) | NULL | argon2id. NULL — пароль ещё не задан (создан админом) или сброшен. |
| `role` | TEXT | NOT NULL DEFAULT `'group_member'`, CHECK IN (`super_admin`,`group_leader`,`group_member`) | Роль. `seed_admin` upsert'ит `super_admin`. |
| `team_id` | BIGINT | NULL, FK → `teams(id)` ON DELETE SET NULL, **DEFERRABLE INITIALLY DEFERRED** | **Домашняя (home/primary) команда** пользователя ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)). Источник инварианта роль↔team, правила «первый=лидер» и дефолта команды при добавлении номера. Доп. членства в других командах — в `user_teams` (M:N). DEFERRABLE — из-за циклического FK с `teams.leader_user_id` (создание лидера: INSERT user → INSERT/UPDATE team → UPDATE user.team_id, проверка FK откладывается до COMMIT). |
| `display_name` | TEXT | NULL, CHECK length 1..100 | Человекочитаемое имя для UI; fallback на `username`. |
| `password_reset_required` | BOOLEAN | NOT NULL DEFAULT true | true после seed/create/reset; false после `set-password`. |
| `lockout_until` | TIMESTAMPTZ | NULL | Если > now() — login отклоняется (см. [08-security.md](./08-security.md)). |
| `failed_login_attempts` | INT | NOT NULL DEFAULT 0 | Сброс при успехе/истечении lockout. |
| `last_login_at` | TIMESTAMPTZ | NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Триггер `set_updated_at()`. |

**CHECK-инварианты:**
- `users_role_check` — `role IN ('super_admin','group_leader','group_member')`.
- `users_role_team_invariant` — табличный CHECK:
  ```sql
  CHECK (
      (role = 'super_admin'  AND team_id IS NULL) OR
      (role = 'group_leader' AND team_id IS NOT NULL) OR
      (role = 'group_member' AND team_id IS NOT NULL)
  )
  ```
- `users_username_lower_check` — `username = lower(username)`.
- `users_display_name_length_check` — `display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100`.

**Индексы:** UNIQUE(`username`); **`UNIQUE INDEX users_single_super_admin ON users ((role)) WHERE role='super_admin'`** — partial-UNIQUE, гарантирует, что в БД существует **не более одного** `super_admin` (инвариант «ровно один админ» — defense-in-depth, не только операционная ответственность); `INDEX (team_id) WHERE team_id IS NOT NULL`.

**`seed_admin` и уникальность super_admin:** `seed_admin()` (при старте) upsert'ит super_admin из `ADMIN_LOGIN`/`ADMIN_PASSWORD`. Чтобы смена `ADMIN_LOGIN` не создала **второго** super_admin: seed сначала ищет **существующего** `super_admin` (по partial-индексу выше). Если он есть и его `username` отличается от `ADMIN_LOGIN` — seed **переименовывает** существующую строку (UPDATE `username`, `password_hash`), а не вставляет новую. Если строки нет — INSERT. Partial-UNIQUE индекс — страховка: попытка вставить второго super_admin падает на уровне БД. Детали seed-логики — [08-security.md](./08-security.md) §1.

**Триггер лидерства (defense-in-depth, `users_team_leader_consistency_check`):** AFTER INSERT OR UPDATE OF `role`,`team_id`, DEFERRABLE INITIALLY DEFERRED — при `role='group_leader'` гарантирует существование `teams` с `id = users.team_id` И `leader_user_id = users.id`. Backend валидирует до SQL для понятных кодов ошибок.

---

### `user_teams` (M:N членство, [ADR-0012](./adr/ADR-0012-multi-team-membership.md))

Аддитивная таблица членства «пользователь ↔ команда». **Источник истины** для адресации SMS (`recipients_for_team`), видимости/добавления номеров в `/app` и подсчёта участников команды. `users.team_id` остаётся **домашней** командой (см. `users` выше); `user_teams` дополняет её доп. членствами.

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `user_id` | BIGINT | NOT NULL, FK → `users(id)` **ON DELETE CASCADE** | Участник. Удаление пользователя удаляет все его членства. |
| `team_id` | BIGINT | NOT NULL, FK → `teams(id)` **ON DELETE CASCADE** | Команда. Удаление команды удаляет все членства в ней (само расформирование выполняется штатным disband-flow, см. [05-api-contracts.md](./05-api-contracts.md) §5; CASCADE — safety-net на строки `user_teams`). |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Момент добавления членства. `updated_at`/триггер не нужны — строки append-only (нет мутируемых полей). |

**Constraints:** UNIQUE `(user_id, team_id)` (`uq_user_teams_user_team`) — членство не дублируется; попытка повторного add → `409 membership_already_exists`.

**Индексы:** UNIQUE выше (обслуживает и lookup `(user_id, team_id)` для `exists`); `user_teams_team_id_idx` на `(team_id)` — для `recipients_for_team`/member-списков по команде.

**Инварианты (нормативно, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)):**
- **Home зеркалируется:** для каждого не-`super_admin` с `users.team_id IS NOT NULL` существует ровно одна home-строка `user_teams(user_id, team_id = users.team_id)`. Проставляется `create_user` и синхронизируется `PATCH /api/admin/users/{id}` (move: remove старый home + add новый home) — обе операции в одной транзакции со сменой `users.team_id`.
- **super_admin не имеет членств:** для `role='super_admin'` строк в `user_teams` нет (согласуется с `team_id IS NULL`). Add super_admin в команду запрещён на уровне сервиса (`400 cannot_add_super_admin_to_team`); БД-CHECK не вводится (проверка на стороне приложения достаточна при единственном super_admin).
- **Роль глобальна, лидерство — только на home:** `user_teams` не несёт роли; доп. членство не делает участника лидером доп. команды. UNIQUE-лидер (`teams.leader_user_id`) и CHECK `users_role_team_invariant` остаются на `users.team_id`.
- **Home нельзя удалить через membership-API:** `DELETE .../teams/{team_id}` при `team_id = users.team_id` → `400 cannot_remove_home_membership` (смена домашней — только `PATCH` move). Так CHECK «у member/leader всегда есть команда» не нарушается.

---

### `telegram_links`

Источник — [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md). Связка Telegram-аккаунта (chat_id) с внутренним `users.id`. Активна пока `dead_at IS NULL`.

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `telegram_user_id` | BIGINT | PK | Telegram User.id из подписанного initData (= chat_id для приватного чата). PK → атомарный upsert `ON CONFLICT (telegram_user_id) DO UPDATE`. |
| `user_id` | BIGINT | NOT NULL, FK → `users(id)` ON DELETE CASCADE | Внутренний пользователь. **Без UNIQUE** — один user может иметь несколько привязок (1:N, мягкий потолок `TG_MAX_LINKS_PER_USER`). |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Обновляется при реальной перепривязке/реактивации (см. self-heal в ADR-0004). |
| `dead_at` | TIMESTAMPTZ | NULL | Заполняется при 403/blocked/chat not found от Bot API. Диспетчер пропускает доставку. Обнуляется при следующем успешном `POST /api/telegram/auth` того же tg-user. |

**Индексы:** PK(`telegram_user_id`); `telegram_links_user_id_idx` на `(user_id)` (неуникальный) — для recipient-SQL и форс-отзыва.

---

### `phone_numbers` (было: `twilio_numbers`)

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `phone_number` | TEXT | NOT NULL, UNIQUE | Номер в формате E.164 (нормализуется `normalize_phone`). |
| `team_id` | BIGINT | **NULL**, FK → `teams(id)` **ON DELETE SET NULL** | Команда-владелец номера. **`NULL` = unassigned** (номер в пуле, не привязан к команде; см. [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)). Удаление команды → её номера становятся unassigned (SET NULL), не удаляются. |
| `added_by_user_id` | BIGINT | NULL, FK → `users(id)` ON DELETE SET NULL | Кто добавил (аудит). Любой участник команды; `NULL` для импортированных unassigned-номеров ([07-deployment.md](./07-deployment.md)). |
| `label` | TEXT | NULL | Ярлык. |
| `is_active` | BOOLEAN | NOT NULL DEFAULT true | |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Триггер `set_updated_at()`. |

**Индексы:** UNIQUE(`phone_number`); `INDEX (team_id)` — **непартиальный** (btree индексирует и NULL): обслуживает и фильтр assigned (`team_id = :id`), и unassigned (`team_id IS NULL`) для `GET /api/admin/numbers` ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)).

**Инвариант unassigned (нормативно):** `team_id IS NULL` — легитимное состояние (пул). SMS на unassigned-номер обрабатывается как неизвестный: `inbound_sms.team_id = phone.team_id = NULL` → получателей нет (см. [03-architecture.md](./03-architecture.md) §«Поток приёма»). Инвариант «у номера всегда есть команда» снят [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md).

---

### `inbound_sms` (было: `inbound_messages`)

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `twilio_message_sid` | TEXT | NULL | Twilio `MessageSid`. |
| `from_number` | TEXT | NOT NULL | Нормализованный отправитель. |
| `to_number` | TEXT | NOT NULL | Нормализованный получатель (наш номер). |
| `body` | TEXT | NOT NULL | Текст SMS. |
| `team_id` | BIGINT | NULL, FK → `teams(id)` ON DELETE SET NULL | Команда по номеру-получателю. NULL — неизвестный номер (SMS сохраняется, но не доставляется). |
| `raw_payload` | JSONB | NOT NULL | Полный form-payload webhook (было `raw_payload_json TEXT`). |
| `received_at` | TIMESTAMPTZ | NOT NULL | Время приёма. |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |

**Constraints:** partial-UNIQUE `(twilio_message_sid) WHERE twilio_message_sid IS NOT NULL` (`inbound_sms_sid_uq`) — дедупликация ретраев webhook. NULL-SID (ручные/тестовые) не конфликтуют.

**Индексы:** partial-UNIQUE выше; `INDEX (received_at DESC)`; `INDEX (team_id) WHERE team_id IS NOT NULL`.

---

### `deliveries` (было: `telegram_deliveries`)

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `inbound_sms_id` | BIGINT | NOT NULL, FK → `inbound_sms(id)` ON DELETE CASCADE | |
| `user_id` | BIGINT | NOT NULL, FK → `users(id)` ON DELETE CASCADE | Получатель-владелец. |
| `telegram_user_id` | BIGINT | NOT NULL | Снимок chat_id на момент доставки. **Без FK** (реестр переживает удаление/перепривязку линка). |
| `status` | TEXT | NOT NULL DEFAULT `'pending'`, CHECK IN (`pending`,`sent`,`failed`,`dead`) | `pending`→`sent`/`failed`/`dead`. `failed` ретраится; `dead` — нет (403/blocked). |
| `attempts` | INT | NOT NULL DEFAULT 0 | Инкремент при каждой попытке. |
| `last_error` | TEXT | NULL | Усечённое описание ошибки (без секретов, ≤1000). |
| `sent_at` | TIMESTAMPTZ | NULL | Время успешной доставки. |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | Триггер `set_updated_at()`. |

**Constraints:** UNIQUE `(inbound_sms_id, telegram_user_id)` (`deliveries_sms_chat_uq`) — идемпотентность доставки на конкретный чат. `DeliveryRepository.try_reserve` использует `pg_insert(...).on_conflict_do_nothing().returning(id)`; пустой RETURNING → доставка в этот чат уже была, пропуск.

**Индексы:** UNIQUE выше; `INDEX (status, attempts) WHERE status IN ('pending','failed')` — для retry-loop.

---

### `admin_audit`

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK | |
| `actor_user_id` | BIGINT | NOT NULL | id действующего (обычно super_admin). **Без FK** — запись переживает удаление пользователя. |
| `action` | TEXT | NOT NULL | Enum-string: `admin_login`, `admin_logout`, `create_user`, `reset_password`, `delete_user`, `lockout_triggered`, `team_create`, `team_rename`, `team_delete`, `team_leader_set`, `user_team_change` (смена домашней команды — `PATCH user`), `user_team_add` / `user_team_remove` (доп. членство — [ADR-0012](./adr/ADR-0012-multi-team-membership.md)), `number_added`, `number_removed`, `number_team_assigned` (admin назначил/снял команду у номера — [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)), `telegram_link_created`, `telegram_link_revoked`, `telegram_link_dead_marked`, `telegram_link_rebound`. |
| `target_user_id` | BIGINT | NULL | Затронутый пользователь. |
| `target_username` | TEXT | NULL | Снимок username (на случай delete). |
| `details` | JSONB | NULL | Структурированные детали (`{telegram_user_id, team_id, phone_number, ...}`). |
| `ip` | TEXT | NULL | IP инициатора. |
| `user_agent` | TEXT | NULL | Усечён до 256. |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |

**Индексы:** `INDEX (created_at DESC)`; `INDEX (actor_user_id, created_at DESC)`; `INDEX (target_user_id) WHERE target_user_id IS NOT NULL`. Ретенция — бессрочная (объём ничтожен).

---

### `service_state`

Без изменений относительно SQLite (кроме типа времени).

| Колонка | Тип | Constraints | Описание |
| --- | --- | --- | --- |
| `key` | TEXT | PK | Ключ. |
| `value` | TEXT | NOT NULL | Значение. |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | |

`StateRepository.set` — `pg_insert(...).on_conflict_do_update(index_elements=['key'], ...)`.

---

## Триггеры и функции

- `set_updated_at()` — `RETURNS trigger`, ставит `NEW.updated_at = now()`. BEFORE UPDATE на `teams`, `users`, `phone_numbers`, `deliveries`.
- `users_team_leader_consistency_check()` — constraint-триггер лидерства (см. `users`).
- Все создаются в initial-миграции Alembic; `downgrade` обратимо их удаляет.

## Alembic

- `alembic.ini`, `migrations/env.py` (async engine, `import shared.models`, `target_metadata = Base.metadata`, `compare_type=True`).
- `migrations/versions/<rev>_initial_schema.py` — все таблицы, FK (с DEFERRABLE где указано), CHECK, UNIQUE, partial-индексы, триггеры/функции; обратимый `downgrade`.
- `migrations/versions/20260702_002_phone_numbers_team_nullable.py` — ревизия ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)): `phone_numbers.team_id` → NULLABLE; пересоздать FK `phone_numbers.team_id → teams(id)` с `ON DELETE SET NULL` (вместо `ON DELETE CASCADE`); индекс `(team_id)` остаётся непартиальным. `downgrade`: обратно `NOT NULL` + `ON DELETE CASCADE` (при наличии `team_id IS NULL` строк downgrade завершится ошибкой NOT NULL — ожидаемо, откат применим только на пустом пуле). Обновить SQLAlchemy-модель `phone_numbers` в `shared/models/`.
- `migrations/versions/20260702_003_user_teams.py` — **новая ревизия** ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)), `down_revision = "20260702_002"`. `upgrade`:
  1. `CREATE TABLE user_teams` (колонки/FK `ON DELETE CASCADE` как выше) + UNIQUE `uq_user_teams_user_team (user_id, team_id)` + `INDEX user_teams_team_id_idx (team_id)`.
  2. **Backfill home-членств** — идемпотентно перенести действующие домашние команды:
     ```sql
     INSERT INTO user_teams (user_id, team_id)
     SELECT id, team_id FROM users WHERE team_id IS NOT NULL
     ON CONFLICT (user_id, team_id) DO NOTHING;
     ```
     (`super_admin` имеет `team_id IS NULL` → не попадает; инвариант «super_admin без членств» соблюдён.)
  `users.team_id` **НЕ** удаляется (остаётся домашней командой). `downgrade`: `DROP TABLE user_teams` (данные членств теряются — обратимо только структурно, домашние команды сохранены в `users.team_id`). Зарегистрировать модель `user_teams` в `shared/models/__init__.py`.
- Требование: `alembic revision --autogenerate` после применения даёт **пустой** diff (см. [06-testing-strategy.md](./06-testing-strategy.md)).

---

## Маппинг SQLite → PostgreSQL

Полный алгоритм — [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md); скрипт `scripts/migrate_sqlite_to_pg.py`.

| SQLite (старое) | PostgreSQL (новое) | Примечания |
| --- | --- | --- |
| `projects` | `teams` | `name`→`name`, `description` отбрасывается (нет колонки; при необходимости → `Q`). `leader_user_id` проставляется на шаге лидеров. |
| `telegram_users` | `users` + `telegram_links` | `users.username='tg_'+telegram_id`, `password_hash=NULL`, `role='group_member'`. `telegram_links(telegram_user_id=telegram_id, user_id=new_id, dead_at=now() если !is_active)`. Маппинг `old telegram_users.id → new user_id`. |
| `user_project_access` | `users.team_id` (+ `user_teams`) | Единственный проект → его команда; несколько → первый становится **домашней** (`users.team_id`); осиротевшие (0 проектов) → служебная команда `Legacy` (`--orphan-team-name`). Домашнее членство зеркалируется в `user_teams` (backfill ревизии `20260702_003`). **Перенос доп. проектных доступов в `user_teams`** технически возможен после [ADR-0012](./adr/ADR-0012-multi-team-membership.md), но one-off `migrate_sqlite_to_pg.py` на этой итерации по-прежнему берёт только первый проект как домашний (`TD-004`); лишние доступы фиксируются в отчёте. Донастройка доп. членств — вручную через membership-API либо будущим расширением скрипта. |
| — (нет) | `teams.leader_user_id` | Лидер = `min(user_id)` среди участников команды; ему `role='group_leader'`. |
| `twilio_numbers` | `phone_numbers` | `team_id←project_id`, `added_by_user_id←leader`. |
| `inbound_messages` | `inbound_sms` | `team_id←project_id`, `raw_payload_json::jsonb`→`raw_payload`. `twilio_message_sid` сохраняется (partial-UNIQUE). |
| `telegram_deliveries` | `deliveries` | `inbound_message_id→inbound_sms_id`; `telegram_user_id` (старое = FK на telegram_users.id) → через маппинг в `deliveries.user_id` + снимок chat_id в `deliveries.telegram_user_id`. UNIQUE dedup. |
| `service_state` | `service_state` | 1:1, кроме `telegram_offset` (long polling удалён). |

**Инварианты после миграции:** у каждой непустой команды ровно один `group_leader`; нет `group_member`/`group_leader` с `team_id IS NULL`; повторный прогон скрипта не создаёт дублей (`ON CONFLICT DO NOTHING`, сохранение id, `setval` sequences).

> `Q-DATA-1` (см. [99-open-questions.md](./99-open-questions.md)): сохранять ли `projects.description` (добавить `teams.description`)? По умолчанию — не сохранять (в текущем UI не используется).

### Отдельный импорт unassigned-номеров ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md))

Не путать с полной миграцией выше. Одноразовый скрипт `scripts/import_numbers.py` переносит из SQLite `twilio_numbers` **только** `phone_number`, `label`, `is_active` в `phone_numbers` как **unassigned** (`team_id=NULL`, `added_by_user_id=NULL`), идемпотентно `INSERT ... ON CONFLICT (phone_number) DO NOTHING`. projects/teams/users/deliveries НЕ переносятся. Эксплуатация — [07-deployment.md](./07-deployment.md).
