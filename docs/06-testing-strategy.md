# 06. Testing Strategy

Тесты пишет и запускает qa-агент. Прод-код — backend/frontend. Инструмент — `pytest` + `pytest-asyncio`.

## Пирамида

| Уровень | Что покрывает | Инфраструктура |
| --- | --- | --- |
| **Unit** | `verify_init_data` (HMAC/TTL), `normalize_phone`, `format_sms_message`, `_split_message`, политика паролей, argon2 wrapper, инварианты ролей. | Без внешних зависимостей; моки. |
| **Integration (DB)** | Репозитории на реальном PostgreSQL, миграции Alembic, CHECK/UNIQUE/DEFERRABLE FK/триггеры, `try_reserve` идемпотентность, `recipients_for_team`. | PostgreSQL (Docker/тест-БД), Redis (или fakeredis). |
| **Contract/API** | Endpoints через `httpx.AsyncClient`/TestClient: webhook, auth flow, Mini App SSO, admin/teams/numbers, guards, CSRF, коды ошибок. | app + PG + Redis; Telegram Bot API и Twilio — моки. |
| **Migration** | Обратимость схемы и скрипт данных `migrate_sqlite_to_pg.py` на копии `service.db`. | PG + временная SQLite. |

## Coverage gate

- Целевое покрытие: **≥80%** по `app/`, `shared/`, `scripts/`.
- Критические модули (sms-пайплайн `services.py`, `auth_service.py`, `telegram_sso_service.py`, `init_data.py`) — **≥90%**.
- Падение тестов из-за бага в коде → возврат исполнителю на rework (blame `code`); из-за расхождения docs↔код → эскалация CU (blame `spec`).

## Ключевые сценарии (из Verification плана)

### Схема / миграции
1. `alembic upgrade head` → `downgrade base` → `upgrade head` — обратимость без ошибок.
2. После `upgrade head` `alembic revision --autogenerate` даёт **пустой** diff (модели ↔ миграция согласованы).
3. Проверка наличия всех CHECK (`users_role_team_invariant`, `users_username_lower_check`), UNIQUE (`inbound_sms_sid_uq`, `deliveries_sms_chat_uq`, `phone_numbers.phone_number`), триггеров (`set_updated_at`, лидерство), DEFERRABLE FK `users.team_id`.

### Приём SMS
4. Seed: команда T + user U (`team_id=T`, `role=group_leader`) + `telegram_links(U, chat_id, dead_at=NULL)` + номер N (`team_id=T`). `POST /api/webhooks/twilio/sms` (`MessageSid=SMt1`, `From`, `To=N`, `Body=hi`, `VERIFY_TWILIO_SIGNATURE=false`) → `200`; в БД: 1 `inbound_sms(team_id=T)` + 1 `deliveries(status=sent)`; мок `send_message` вызван с `chat_id`.
5. **Мульти-получатель:** второй user той же команды с живой привязкой → 2 `deliveries`.
6. **Идемпотентность:** повтор того же `MessageSid` → нет новых `inbound_sms`/`deliveries` (partial-UNIQUE + `try_reserve`).
7. **Неизвестный номер:** `To` без `phone_numbers` → `inbound_sms(team_id=NULL)`, 0 доставок, `200`.
8. **403 / dead-link:** мок `TelegramForbiddenError` → `deliveries.status='dead'` + `telegram_links.dead_at` заполнен; retry-loop не берёт `dead`.
9. **Retry:** мок временной ошибки → `deliveries.status='failed'`; retry-loop повторяет; успех → `sent`.
9a. **Crash-recovery fan-out (ADR-0005 §4).** Команда с 2 получателями (U1, U2). Симулировать обрыв после доставки U1 и до U2 (напр. `send_message` бросает у U2 / процесс прерван): в БД `deliveries` для U1 = `sent`, для U2 отсутствует или `pending`/`failed`. Затем повтор webhook с тем же `MessageSid` (дедуп-ветка) — проверить, что **не** делается ранний возврат: `recipients_for_team` + `try_reserve` вызваны снова, U1 не дублируется (idempotent), U2 добирается → `sent`. Альтернативно: `delivery_retry_loop` добирает U2. Итог: обоим доставлено ровно по одному разу, дублей нет.

### Auth
10. `seed_admin`: первый старт → лог `created`; рестарт с тем же env → `unchanged` (идемпотентность).
11. Двухэтапный логин: `POST /login` → `303 /login/password`; `POST /login/password` (верный пароль) → `303 /`.
11a. **Корневой маршрут и per-role landing (ADR-0008).** `GET /` без сессии → `302 /login`; с сессией `super_admin` → `302 /admin`; с сессией `group_leader`/`group_member` → `302 /app`. Затем **у каждой роли landing отдаёт `200`**: `GET /admin` (super_admin) → `200`; `GET /app` (leader/member) → `200` и содержит номера своей команды + статус Telegram-привязки + форму добавления номера + logout. `GET /app` для `super_admin` → `302 /admin`. Инвариант: после успешного логина ни одна роль не получает `404`.
12. Создание пользователя админом → `password_hash IS NULL`, `password_reset_required=true`; первый вход → редирект на `/set-password`; после `POST /set-password` → `303 /`, `password_reset_required=false`.
13. `reset` → revoke сессий + всех `telegram_links`; `delete` — каскад; попытка удалить лидера непустой команды → `409`.
14. Анти-энумерация: `POST /login` для несуществующего и существующего логина → одинаковый ответ/тайминг.
15. Guards: `group_member` → `POST /api/admin/users` = `403`; `POST /api/numbers` в свою команду = `201`.
16. Lockout: N неверных паролей (`LOGIN_FAILURE_THRESHOLD`) → `lockout_until` установлен, следующий вход отклонён до истечения.

### Mini App SSO
17. Валидный `initData`, без сессии, без привязки → `200 {linked:false}` + `sms_tg_pending`; после `POST /login/password` — `telegram_links` создан.
18. Валидный `initData`, есть сессия → self-heal → `200 {linked:false, healed:true}`; для уже-живой привязки того же user — NO-OP (строка/`created_at` не меняются, audit не пишется).
19. Протухший `auth_date` → `401 init_data_expired`; подделанный hash → `401 invalid_init_data`; флуд → `429`.

### Миграция данных
20. Прогон `migrate_sqlite_to_pg.py` на копии `service.db` → сверка `COUNT(*)` по таблицам; у каждой непустой команды ровно один `group_leader`; нет `group_member`/`group_leader` с `team_id IS NULL`; повторный прогон — без дублей.

### Smoke
21. `docker compose up` — порядок `postgres(healthy) → migrate(completed) → app`; `GET /health` → `200`.
