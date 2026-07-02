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
3a. **Unassigned-номера ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)):** `phone_numbers.team_id` — NULLABLE; FK `phone_numbers.team_id → teams(id)` имеет `ON DELETE SET NULL` (не CASCADE). Вставка номера с `team_id=NULL` проходит. Удаление команды с номерами → номера сохраняются с `team_id=NULL` (не удалены). Revision применяется/откатывается, autogenerate-diff пуст.

### Приём SMS
4. Seed: команда T + user U (`team_id=T`, `role=group_leader`) + `telegram_links(U, chat_id, dead_at=NULL)` + номер N (`team_id=T`). `POST /api/webhooks/twilio/sms` (`MessageSid=SMt1`, `From`, `To=N`, `Body=hi`, `VERIFY_TWILIO_SIGNATURE=false`) → `200`; в БД: 1 `inbound_sms(team_id=T)` + 1 `deliveries(status=sent)`; мок `send_message` вызван с `chat_id`.
5. **Мульти-получатель:** второй user той же команды с живой привязкой → 2 `deliveries`.
6. **Идемпотентность:** повтор того же `MessageSid` → нет новых `inbound_sms`/`deliveries` (partial-UNIQUE + `try_reserve`).
7. **Неизвестный номер:** `To` без `phone_numbers` → `inbound_sms(team_id=NULL)`, 0 доставок, `200`.
7a. **Unassigned-номер ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)):** номер существует с `team_id=NULL` → `inbound_sms(team_id=NULL)`, `recipients_for_team` не вызывается, **0 доставок**, `200` (эквивалентно неизвестному номеру).
8. **403 / dead-link:** мок `TelegramForbiddenError` → `deliveries.status='dead'` + `telegram_links.dead_at` заполнен; retry-loop не берёт `dead`.
9. **Retry:** мок временной ошибки → `deliveries.status='failed'`; retry-loop повторяет; успех → `sent`.
9a. **Crash-recovery fan-out (ADR-0005 §4).** Команда с 2 получателями (U1, U2). Симулировать обрыв после доставки U1 и до U2 (напр. `send_message` бросает у U2 / процесс прерван): в БД `deliveries` для U1 = `sent`, для U2 отсутствует или `pending`/`failed`. Затем повтор webhook с тем же `MessageSid` (дедуп-ветка) — проверить, что **не** делается ранний возврат: `recipients_for_team` + `try_reserve` вызваны снова, U1 не дублируется (idempotent), U2 добирается → `sent`. Альтернативно: `delivery_retry_loop` добирает U2. Итог: обоим доставлено ровно по одному разу, дублей нет.

### Auth
10. `seed_admin`: первый старт → лог `created`; рестарт с тем же env → `unchanged` (идемпотентность).
11. Двухэтапный логин (амендмент [ADR-0002](./adr/ADR-0002-two-step-login.md)): `POST /login` для активного аккаунта с паролем → `303 /login/password` (+`sms_login`); `POST /login/password` (верный пароль) → `303 /` (+`sms_session`/`sms_csrf`; при наличии — clear `sms_logged_out`).
11a. **Корневой маршрут и per-role landing (ADR-0008).** `GET /` без сессии → `302 /login`; с сессией `super_admin` → `302 /admin`; с сессией `group_leader`/`group_member` → `302 /app`. Затем **у каждой роли landing отдаёт `200`**: `GET /admin` (super_admin) → `200`; `GET /app` (leader/member) → `200` и содержит номера своей команды + статус Telegram-привязки + форму добавления номера + logout. `GET /app` для `super_admin` → `302 /admin`. Инвариант: после успешного логина ни одна роль не получает `404`.
12. Создание пользователя админом → `password_hash IS NULL`, `password_reset_required=true`; первый вход: **шаг-1 `POST /login` сразу → `303 /set-password`** (+`sms_setup`, «придумай пароль», амендмент [ADR-0002](./adr/ADR-0002-two-step-login.md)) — форма ввода пароля НЕ показывается; после `POST /set-password` → `303 /`, `password_reset_required=false`. Fallback: если такой аккаунт попал на `POST /login/password`, шаг-2 тоже перенаправляет на `/set-password`.
13. `reset` → revoke сессий + всех `telegram_links`; `delete` — каскад; попытка удалить лидера непустой команды → `409`.
14. Мягкая анти-энумерация ([08](./08-security.md) §6): `POST /login` для **несуществующего** логина → `303 /login/password` (идентично **активному** аккаунту с паролем, одинаковый тайминг); шаг-2 → `invalid_credentials`. Аккаунт в состоянии `password_reset_required` различим (→ `/set-password`) — принятый риск TD-010.
15. Guards: `group_member` → `POST /api/admin/users` = `403`; `POST /api/numbers` в свою команду = `201`.
16. Lockout: N неверных паролей (`LOGIN_FAILURE_THRESHOLD`) → `lockout_until` установлен, следующий вход отклонён до истечения.
16a. **«Залипающий» logout ([ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)).** `POST /logout` → `302 /login` + `Set-Cookie sms_logged_out`. При наличии `sms_logged_out` и живой привязке `POST /api/telegram/auth` (без сессии) → `200 {linked:false, logged_out:true}` **без** `sms_session` (пользователь остаётся разлогинен, `telegram_links` не тронуты). После явного входа (маркер сброшен) тот же вызов при живой привязке → `200 {linked:true, redirect:"/"}` + `sms_session`. При активной сессии stale-`sms_logged_out` очищается.

### Mini App SSO
17. Валидный `initData`, без сессии, без привязки → `200 {linked:false}` + `sms_tg_pending`; после `POST /login/password` — `telegram_links` создан.
18. Валидный `initData`, есть сессия → self-heal → `200 {linked:false, healed:true}`; для уже-живой привязки того же user — NO-OP (строка/`created_at` не меняются, audit не пишется).
19. Протухший `auth_date` → `401 init_data_expired`; подделанный hash → `401 invalid_init_data`; флуд → `429`.

### Миграция и импорт данных
20. Прогон `migrate_sqlite_to_pg.py` на копии `service.db` → сверка `COUNT(*)` по таблицам; у каждой непустой команды ровно один `group_leader`; нет `group_member`/`group_leader` с `team_id IS NULL`; повторный прогон — без дублей.
21. **Импорт номеров `import_numbers.py` ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)):** прогон на копии `service.db` → все `twilio_numbers` появились в `phone_numbers` с `team_id=NULL`, `added_by_user_id=NULL`, сохранены `phone_number`/`label`/`is_active`; projects/teams/users/deliveries НЕ созданы. **Идемпотентность:** повторный прогон — 0 новых строк (ON CONFLICT phone_number DO NOTHING); уже назначенные (team_id≠NULL) номера не перезаписываются.

### Admin — группировка пользователей (Задача 1)
22. **Сортировка `GET /api/admin/users` ([05](./05-api-contracts.md) §4):** сначала `super_admin`; затем по `team_name`; внутри команды — лидер первым (`is_leader=true`), затем участники по `username`. Поле `is_leader` корректно (true только для `teams.leader_user_id`).
23. **SSR `GET /admin`:** контекст содержит `super_admins` (секция первой), `team_sections` (по `team_name`, лидер помечен и первый в `members`), `unassigned_numbers`, `teams`, `csrf_token`. Рендер `200`; лидер визуально помечен; секция администраторов — раньше командных.

### Admin — номера и распределение (Задача 2, [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md))
24. `GET /api/admin/numbers?assignment=unassigned` → только `team_id IS NULL`; `?assignment=assigned` → только `team_id IS NOT NULL`; `?team_id=<id>` → номера команды; `all` → все. Конфликт `team_id` + `assignment=unassigned` → `400 invalid_query`. Guard: `group_member`/`group_leader` → `403`.
25. `PATCH /api/admin/numbers/{id}` `{team_id: T}` → номер привязан (audit `number_team_assigned`); `{team_id: null}` → снят (unassigned); несуществующий номер → `404 number_not_found`; несуществующая команда → `404 team_not_found`; не-админ → `403`.
26. **Удаление команды сохраняет номера:** команда с номерами → `DELETE /api/admin/teams/{id}` (после опустошения от пользователей) → номера получают `team_id=NULL`, не удалены.

### Telegram webhook (Задача 3, [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md))
27. `POST /api/telegram/webhook` с верным `X-Telegram-Bot-Api-Secret-Token` и `message.text="/start"` → `200`; мок `send_message` вызван с `chat_id` и кнопкой `web_app` (url=`TELEGRAM_WEBAPP_URL`).
28. Неверный/отсутствующий секрет-токен → `403 invalid_webhook_secret`, `send_message` не вызван, тело не обрабатывается.
29. Прочий апдейт (иной текст / callback / edited_message) с верным секретом → `200`, `send_message` не вызван (no-op). `/help` и прочие команды не обрабатываются.
30. Ошибка `send_message` (мок сети) при `/start` → обработчик всё равно `200` (не роняется); секрет-токен/тело не попадают в логи.
31. Флуд по IP → `429` (rate-limit §4).

### Smoke
32. `docker compose up` — порядок `postgres(healthy) → migrate(completed) → app`; `GET /health` → `200`.
