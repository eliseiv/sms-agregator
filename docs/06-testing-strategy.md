# 06. Testing Strategy

Тесты пишет и запускает qa-агент. Прод-код — backend/frontend. Инструмент — `pytest` + `pytest-asyncio`.

## Пирамида

| Уровень | Что покрывает | Инфраструктура |
| --- | --- | --- |
| **Unit** | `verify_init_data` (HMAC/TTL), `normalize_phone`, `format_sms_message`, `_split_message`, политика паролей, argon2 wrapper, инварианты ролей. | Без внешних зависимостей; моки. |
| **Integration (DB)** | Репозитории на реальном PostgreSQL, миграции Alembic, CHECK/UNIQUE/DEFERRABLE FK/триггеры, `try_reserve` идемпотентность, `recipients_for_team` (через `user_teams`), `UserTeamRepository` (add/remove/list_team_ids), backfill home-членств. | PostgreSQL (Docker/тест-БД), Redis (или fakeredis). |
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
3b. **`user_teams` и backfill ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)):** ревизия `20260702_003_user_teams` создаёт таблицу с UNIQUE `(user_id, team_id)` + `INDEX(team_id)`, FK `user_id`/`team_id` — `ON DELETE CASCADE`. После `upgrade` **backfill**: число строк `user_teams` = число пользователей с `team_id IS NOT NULL` (super_admin с `team_id IS NULL` не попал); каждая home-строка = `(user_id, users.team_id)`. `upgrade→downgrade→upgrade` без ошибок; `downgrade` дропает таблицу. Повторный `INSERT ... ON CONFLICT DO NOTHING` дублей не создаёт. autogenerate-diff после upgrade — **пуст** (модель `user_teams` ↔ миграция согласованы). Удаление пользователя → каскадом удаляются его строки `user_teams`; удаление команды → каскадом её строки.

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
23. **SSR `GET /admin` (паритет [ADR-0015](./adr/ADR-0015-admin-users-visual-parity-with-mail-agregator.md)):** контекст содержит `user_groups` (бакеты по домашней команде; бакет «без команды»/super_admin первым, далее по `team_name`, внутри — лидер первым), у каждого `<user>` — `home_team` + `memberships`, а также `teams`, `q`, `total`, `page`, `limit`, `unassigned_numbers`, `csrf_token`. Рендер `200`. **Единая таблица** `admin-users-table` (6 колонок: Имя·Роль·Команда·Создан·Последний вход·Действия — **без** «Telegram»); команды — `<tbody>`-бакеты (не отдельные `<section>`); super_admin в бакете «без команды» (actions «системный»), **без** отдельной секции «Администраторы».

### Admin — номера и распределение (Задача 2, [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md))
24. `GET /api/admin/numbers?assignment=unassigned` → только `team_id IS NULL`; `?assignment=assigned` → только `team_id IS NOT NULL`; `?team_id=<id>` → номера команды; `all` → все. Конфликт `team_id` + `assignment=unassigned` → `400 invalid_query`. Guard: `group_member`/`group_leader` → `403`.
25. `PATCH /api/admin/numbers/{id}` `{team_id: T}` → номер привязан (audit `number_team_assigned`); `{team_id: null}` → снят (unassigned); несуществующий номер → `404 number_not_found`; несуществующая команда → `404 team_not_found`; не-админ → `403`.
26. **Удаление команды сохраняет номера:** команда с номерами → `DELETE /api/admin/teams/{id}` (после опустошения от пользователей) → номера получают `team_id=NULL`, не удалены.

### Admin — on-demand sync номеров из Twilio ([ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md))
Twilio-клиент замокан (без реального сетевого вызова); проверяется поведение хендлера/сервиса.
26a. **Sync добавляет unassigned.** Мок Twilio возвращает набор E.164-номеров, часть отсутствует в БД → `POST /api/admin/numbers/sync` (super_admin, CSRF) → `200 {synced_total, added, skipped_existing}`; новые строки `phone_numbers` с `team_id=NULL`, `added_by_user_id=NULL`; номера нормализованы (`normalize_phone`), `UNIQUE(phone_number)` соблюдён; audit `numbers_synced` c `details={synced_total, added, skipped_existing}`.
26b. **Идемпотентность / skip existing.** Повторный `POST .../sync` тем же набором → `added=0`, `skipped_existing=synced_total`, дублей нет. **Назначенный команде номер не трогается:** номер, уже привязанный к команде (`team_id=T`), присутствует в ответе Twilio → после sync его `team_id` остаётся `T` (не обнулён), `label`/`added_by_user_id` не изменены (`ON CONFLICT DO NOTHING`).
26c. **Пагинация.** Мок Twilio отдаёт номера **несколькими страницами** → sync собирает **все** страницы (`synced_total` = сумма по страницам), ни одна не потеряна.
26d. **`twilio_error`.** Мок Twilio бросает ошибку/таймаут → `502`/`503 {"error":"twilio_error"}`; секреты не в логах; частичный прогон не создаёт дублей (повтор безопасен). При незаданных `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` → `503 {"error":"twilio_not_configured"}` (Twilio не вызывается).
26e. **Только super_admin.** `group_member`/`group_leader` → `403`; аноним → `401`/redirect; без CSRF-токена при активной сессии → отклонение (double-submit). Никаких авто-назначений: после sync все новые номера — в unassigned-пуле, доставок по ним нет до `PATCH /api/admin/numbers/{id}`.
26f. **CLI-паритет.** `scripts/sync_twilio_numbers.py` (мок Twilio) даёт тот же результат, что endpoint: upsert unassigned, идемпотентность, счётчики в отчёте.

### Telegram webhook (Задача 3, [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md))
27. `POST /api/telegram/webhook` с верным `X-Telegram-Bot-Api-Secret-Token` и `message.text="/start"` → `200`; мок `send_message` вызван с `chat_id` и кнопкой `web_app` (url=`TELEGRAM_WEBAPP_URL`).
28. Неверный/отсутствующий секрет-токен → `403 invalid_webhook_secret`, `send_message` не вызван, тело не обрабатывается.
29. Прочий апдейт (иной текст / callback / edited_message) с верным секретом → `200`, `send_message` не вызван (no-op). `/help` и прочие команды не обрабатываются.
30. Ошибка `send_message` (мок сети) при `/start` → обработчик всё равно `200` (не роняется); секрет-токен/тело не попадают в логи.
31. Флуд по IP → `429` (rate-limit §4).

### Multi-team ([ADR-0012](./adr/ADR-0012-multi-team-membership.md))
32. **`recipients_for_team` через членство.** Команды T1, T2. Пользователь U: домашняя T1 (`users.team_id=T1`, backfilled home в `user_teams`), доп. членство в T2 (`user_teams(U,T2)`), живая `telegram_links`. Номер N2 в T2. `POST /api/webhooks/twilio/sms` на N2 → U **получает** (recipients через `user_teams`, а не `users.team_id`): 1 `delivery(status=sent)` для U. Контроль: пользователь V только в T1 (не в T2) SMS на N2 **не** получает.
33. **Add членства.** `POST /api/admin/users/{U}/teams {team_id:T2}` (super_admin) → `201`; строка `user_teams(U,T2)`; сессии U ревокнуты (повторный запрос требует нового входа / scope перечитан). Повтор → `409 membership_already_exists`. Роль U и `teams.leader_user_id` не изменились. Не-админ → `403`. super_admin как target → `400 cannot_add_super_admin_to_team`. Несуществующий user/team → `404 user_not_found`/`team_not_found`.
34. **Remove членства.** После доп. членства `DELETE /api/admin/users/{U}/teams/{T2}` → `204`; строки нет; сессии ревокнуты; U перестаёт получать SMS T2. Попытка удалить **домашнюю** (`{T1}`) → `400 cannot_remove_home_membership`. Несуществующее членство → `404 membership_not_found`. Form-fallback: `POST` на same-path `/api/admin/users/{U}/teams/{T2}` с `_method=DELETE` (CSRF) → эквивалентно `DELETE` (`204`/redirect).
35. **create_user зеркалит home.** `POST /api/admin/users {team_id:T}` → создан `users.team_id=T` **и** строка `user_teams(new_id, T)` в той же транзакции.
36. **PATCH move синхронизирует членство.** Обычный участник U (home T1) `PATCH /api/admin/users/{U} {team_id:T2}` → `users.team_id=T2`; в `user_teams` **удалена** `(U,T1)`, **добавлена** `(U,T2)`; доп. членства не тронуты; сессии ревокнуты. Инварианты лидера/«первый=лидер» соблюдены; перенос лидера непустой команды → `409` (без изменения `user_teams`).
37. **`GET /api/numbers` и `/app` scope=team_ids.** U в T1+T2, номера в обеих → `GET /api/numbers` возвращает номера **обеих** команд; `GET /app` → `200`, номера сгруппированы по командам, селектор команды содержит T1 и T2. `POST /api/numbers {team_id:T2}` от U → `201`; `POST /api/numbers {team_id:T3}` (не своя) → `403 forbidden`. `DELETE` номера T2 участником U → `200`.
38. **`GET /api/admin/users` / SSR `/admin` multi-team (паритет [ADR-0015](./adr/ADR-0015-admin-users-visual-parity-with-mail-agregator.md)).** Ответ API содержит `team_ids` (home + доп.). SSR `/admin`: U показан **ОДНОЙ строкой** в бакете **домашней** команды; все его команды — **чипами** в колонке «Команда» (`team-chip`; домашний — `team-chip--home` без «×», доп. — с «×»/remove-формой). Дублирования строк на членство **нет**. класс банда бакетов (вычисляется **в шаблоне**, не поле контракта) чередуется (`user-group--band-a`/`--band-b`); бакет «без команды» — `user-group--no-group`. Рендер `200`.
39. **Ревокация сессий (нормативно).** После add/remove/move членства активная сессия target инвалидируется (`revoke_all_for_user`) — старый `VisibilityScope.team_ids` не переиспользуется (stale scope исключён). **DELETE команды с доп.-участниками:** участник W с доп. членством в T2 (`user_teams(W,T2)`, домашняя — T1). После штатного disband и `DELETE /api/admin/teams/{T2}` (CASCADE снял строку `user_teams(W,T2)`) — сессии W ревокнуты (`revoke_all_for_user(W)`): в новом scope `team_ids` не содержит T2.

### Подсветка команд — banding (UI)
40. **CSP-safe banding (паритет [ADR-0015](./adr/ADR-0015-admin-users-visual-parity-with-mail-agregator.md)).** `/admin` с ≥2 командами: `<tbody>`-бакеты получают чередующиеся классы `user-group--band-a`/`--band-b`, бакет «без команды»/super_admin — `user-group--no-group`, между бакетами — `user-group__spacer`; подсветка задаётся **только классами** (нет inline-style), CSP не нарушается. Схема БД без `teams.color`.

### Просмотр входящих SMS ([ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md))
42. **Ролевая видимость super_admin.** SMS на номера разных команд + на номер вне команд (unassigned) + на удалённый номер. `GET /api/messages` от super_admin → `200`, видны **все** SMS. Фильтр `?to_number=` → только этот номер. Фильтр `?team_id=T` → только SMS номеров, **сейчас** принадлежащих T (`phone_numbers.team_id=T`).
43. **Ролевая видимость участника — по текущей принадлежности.** Команды T1(номер N1), T2(номер N2). Участник U — только в T1. SMS есть на N1 и N2. `GET /api/messages` от U → видны SMS только N1; SMS N2 отсутствуют.
44. **Текущая принадлежность vs снимок (ключевой сценарий).** SMS приняты на номер N, когда N был в T2 (или unassigned) → `inbound_sms.team_id` = T2/NULL (снимок). Затем N переназначен в T1. Участник U (в T1) `GET /api/messages` → **видит** исторические SMS N (доступ по текущему `phone_numbers.team_id=T1`, не по снимку). Контроль: участник V (в T2, куда N больше не принадлежит) те же SMS **не** видит.
45. **Анти-энумерация.** Участник U (в T1) запрашивает `?to_number=<номер чужой команды T2>` → `200` c пустым `messages` (не `403`/`404`); существование SMS не раскрыто.
46. **Пустой scope.** Участник без команд (`scope.team_ids` пуст) → `GET /api/messages` → `200`, `messages: []`.
47. **Keyset-пагинация.** ≥ (limit+1) SMS у видимого номера. Первый запрос `?limit=N` → `N` элементов, `next_cursor != null`, порядок `received_at DESC, id DESC`. Второй запрос с этим `cursor` → следующая страница **без пропусков и дублей** (в т.ч. проверка tie-break `id` на строках с равным `received_at`). Последняя страница → `next_cursor = null`.
48. **Битый курсор.** `?cursor=<мусор/невалидный base64url>` → `400 {"error":"invalid_cursor"}`.
49. **Невалидный limit.** `?limit=0` и `?limit=101` → `400 {"error":"invalid_limit"}`; `limit` не передан → дефолт 50.
50. **raw_payload не раскрыт.** В сериализованном SMS отсутствует `raw_payload`; присутствует подмножество `{id, from_number, to_number, body, received_at, team_id}` ([05-api-contracts.md](./05-api-contracts.md) §9).
51. **SSR `GET /messages` — рендер и no-JS fallback.** Для каждой роли (super_admin, leader, member) `GET /messages` → `200` (рендер, не редирект). Без JS: GET-форма фильтра с `<select>` номера (для super_admin — ещё `<select>` команды) сабмитом меняет выборку; ссылка «Дальше» ведёт на `/messages?...&cursor=<next_cursor>` и отдаёт `200` со следующей страницей; при `next_cursor=null` ссылки «Дальше» нет. Аноним → `302 /login`.
52. **Anti-CSRF / read-only.** У `/api/messages` и `/messages` нет mutate-эндпоинтов; `GET` без CSRF; POST-форм (кроме logout из `base.html`) страница не содержит.

### Smoke
53. `docker compose up` — порядок `postgres(healthy) → migrate(completed) → app`; `GET /health` → `200`.
