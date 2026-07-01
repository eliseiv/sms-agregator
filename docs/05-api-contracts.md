# 05. API Contracts

Все имена таблиц/полей/ролей согласованы с [04-data-model.md](./04-data-model.md); безопасность — [08-security.md](./08-security.md).

## Соглашения

- **Cookies:** `sms_session` (сессия, HttpOnly), `sms_csrf` (double-submit, читается JS), `sms_setup` (setup-сессия при первом входе), `sms_login` (промежуточная сессия шага-1 логина), `sms_tg_pending` (pending-токен Mini App SSO, HttpOnly, короткоживущий).
- **CSRF:** double-submit применяется к небезопасным методам (`POST/DELETE/PATCH`) **только когда у запроса уже есть cookie с CSRF-токеном** (`sms_csrf` при аутентифицированной `sms_session`, или `sms_setup`+CSRF на `/set-password`). Проверяется совпадение заголовка/поля `csrf_token` с cookie. **Exempt** (double-submit невозможен или не нужен): `POST /api/webhooks/twilio/sms` (подпись Twilio), `POST /api/telegram/auth` (HMAC initData), а также **`POST /login` и `POST /login/password`** — на этих шагах сессии и cookie `sms_csrf` ещё нет, поэтому CSRF-токен физически невозможен; их защищает rate-limit (`LIMIT_LOGIN` per IP / `LIMIT_LOGIN_USERNAME` per username, см. [08-security.md](./08-security.md) §4). Как только устанавливается setup-сессия (`/set-password`) или полноценная сессия (admin/numbers/logout) — CSRF обязателен.
- **Ошибки API (`/api/*`):** JSON `{"error": "<code>", "detail": "<человекочит.>"}` + соответствующий HTTP-код. Ошибки SSR-страниц (`/login`, `/admin`) — редирект/ре-рендер с флеш-сообщением.
- **Guards (`app/api/deps.py`):**
  - `require_authenticated` — есть валидная `sms_session`.
  - `require_admin` — `role == super_admin`.
  - `require_admin_or_leader` — `role IN (super_admin, group_leader)`.
  - `VisibilityScope{user_id, role, team_id}` — область видимости (super_admin видит всё; остальные — свою команду).
- **NotAuthenticatedError handler:** `/api/*` → `401` JSON; прочее → `302 /login`.

---

## 1. Webhook Twilio

### `POST /api/webhooks/twilio/sms`
- **Доступ:** публичный. **CSRF:** exempt. **Auth:** подпись Twilio.
- **Content-Type:** `application/x-www-form-urlencoded`.
- **Тело (Twilio-поля):** `MessageSid`, `From`, `To`, `Body`, + прочие поля Twilio (сохраняются целиком в `inbound_sms.raw_payload`).
- **Логика:** валидация подписи (если `VERIFY_TWILIO_SIGNATURE=true`) по `X-Twilio-Signature` → `handle_incoming_sms` (см. [03-architecture.md](./03-architecture.md)).
- **Ответы:**
  - `200` — `<Response></Response>` (`application/xml`). Всегда при успешной обработке, включая неизвестный номер (`team_id=NULL`, доставок нет) и дубликат по `MessageSid` (без новых доставок).
  - `401` — неверная подпись (`invalid_twilio_signature`).
  - `500` — `VERIFY_TWILIO_SIGNATURE=true`, но `TWILIO_AUTH_TOKEN` не настроен.

---

## 2. Auth (двухэтапный логин, SSR)

Источник — [ADR-0002](./adr/ADR-0002-two-step-login.md). Анти-энумерация: несуществующий логин отвечает так же, как существующий (`ready_for_password`).

### `GET /login`
- SSR-форма шага-1 (ввод логина). Если Mini App прислал `sms_tg_pending` — cookie уже установлен `POST /api/telegram/auth`.

### `POST /login` (шаг 1 — логин)
- **CSRF:** exempt (сессии/`sms_csrf` ещё нет; защита — rate-limit).
- **Тело (form):** `username`.
- **Логика:** нормализует username (lower), `lookup_for_login`. Устанавливает `sms_login` (промежуточную сессию с username). Ответ единый для существующего и несуществующего логина.
- **Ответы:** `303 → /login/password` (+ Set-Cookie `sms_login`). При превышении rate-limit — `429`.

### `GET /login/password`
- SSR-форма шага-2 (ввод пароля). Требует `sms_login`; иначе `302 → /login`.

### `POST /login/password` (шаг 2 — пароль)
- **CSRF:** exempt (при `sms_login` ещё нет `sms_csrf`; защита — rate-limit).
- **Тело (form):** `password`.
- **Логика:** `login()` — argon2 verify с анти-timing dummy-hash для несуществующего/без пароля; учёт `failed_login_attempts`/`lockout_until`; `needs_rehash` → перехеширование. При `password_reset_required=true` или `password_hash IS NULL` → направление на `/set-password`. При успехе — создание сессии `sms_session`/`sms_csrf`, `link_pending` если есть `sms_tg_pending`.
- **Ответы:**
  - `303 → /` — успех (Set-Cookie `sms_session`, `sms_csrf`; clear `sms_login`).
  - `303 → /set-password` — требуется установка пароля (Set-Cookie `sms_setup`).
  - `200`/re-render с ошибкой — неверный пароль (`invalid_credentials`).
  - `423` (или re-render) — аккаунт заблокирован (`account_locked`), до `lockout_until`.
  - `429` — rate-limit.

### `GET /set-password`
- SSR-форма установки пароля. Требует `sms_setup`; иначе `302 → /login`.

### `POST /set-password`
- **CSRF:** да — на этом шаге установлена setup-сессия `sms_setup` и парный CSRF-cookie, double-submit применяется.
- **Тело (form):** `password`, `password_confirm`, `csrf_token`.
- **Логика:** `complete_set_password` — валидация политики пароля (см. [08-security.md](./08-security.md)), argon2 hash, `password_reset_required=false`. Создание сессии; `link_pending` если есть `sms_tg_pending`.
- **Ответы:** `303 → /` (Set-Cookie `sms_session`,`sms_csrf`; clear `sms_setup`); `200`/re-render при слабом/несовпадающем пароле (`weak_password`/`password_mismatch`); `429` — rate-limit.

### `POST /logout`
- **Тело (form):** `csrf_token`.
- **Логика:** revoke `sms_session` (Redis), clear cookies. **`telegram_links` НЕ трогаются** (push переживает logout — см. [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)). Для `super_admin` — audit `admin_logout`.
- **Ответы:** `302 → /login`.

---

## 3. Telegram Mini App SSO

### `POST /api/telegram/auth`
- **Доступ:** публичный. **CSRF:** exempt (защита — HMAC initData). Источник — [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md).
- **Content-Type:** `application/json`. **Тело:** `{"init_data": "<raw initData>"}`.
- **Rate-limit:** `TG_AUTH` per IP до HMAC; per `telegram_user_id` после HMAC (см. [08-security.md](./08-security.md)).
- **Логика:** `verify_init_data` (HMAC-SHA256 + `auth_date` TTL). Ветвление:
  - **есть активная `sms_session`** → `self_heal_link` (idempotent upsert привязки к текущему user; NO-OP для живой привязки того же user) → `200 {"linked": false, "healed": true}` (без redirect, без cookie).
  - **нет сессии, привязка существует** (`telegram_links` с `dead_at IS NULL`) → создать сессию → `200 {"linked": true, "redirect": "/"}` + Set-Cookie `sms_session`,`sms_csrf`.
  - **нет сессии, привязки нет** → `create_pending` (Redis) → `200 {"linked": false}` + Set-Cookie `sms_tg_pending`.
- **Ответы ошибок:** `401 {"error":"invalid_init_data"}` (плохой HMAC) / `{"error":"init_data_expired"}` (протух `auth_date`); `429` — rate-limit; при внутренней ошибке self-heal — `200 {"linked": false, "healed": false}` (best-effort, фронт не перезагружается).

---

## 4. Admin — пользователи

Все — `require_admin`. Scope-guard: super_admin действует над любыми пользователями.

### `GET /api/admin/users`
- **Ответ `200`:** `{"users": [{"id","username","display_name","role","team_id","team_name","password_reset_required","has_telegram_link","created_at","last_login_at"}]}`.

### `POST /api/admin/users`
- **Тело (JSON):** `{"username": str, "display_name": str|null, "team_id": int}`. **`team_id` обязателен** — создаваемый пользователь получает роль `group_member`/`group_leader`, для которой CHECK `users_role_team_invariant` требует `team_id IS NOT NULL` ([04-data-model.md](./04-data-model.md)). Создать пользователя «без команды» нельзя (роль `super_admin` не создаётся через этот endpoint — он seed-only).
- **Логика:** `create_user` — `username` (lower, уникальный), `password_hash=NULL`, `password_reset_required=true`. Пользователь добавляется в команду `team_id` в **одной транзакции** (deferred constraints): срабатывает правило «первый=лидер» (см. §5) — если команда была пустой (`leader_user_id IS NULL`), пользователь становится `group_leader` (`teams_service.create_for_leader`/`set_leader_if_absent`), иначе `group_member`. `team_id` проставляется атомарно, поэтому CHECK-инвариант не нарушается. Audit `create_user`.
- **Ответы:** `201 {"id", "username", "role", "team_id"}`; `400 {"error":"team_required"}` (team_id отсутствует/пуст); `409 {"error":"username_taken"}`; `400 {"error":"invalid_username"}`; `404 {"error":"team_not_found"}`.

### `POST /api/admin/users/{id}/reset`
- **Логика:** `reset_password` — `password_hash=NULL`, `password_reset_required=true`; revoke всех сессий пользователя (Redis) и **всех** `telegram_links` (`reason="password_reset"`). Audit `reset_password`.
- **Запрет сброса super_admin:** нельзя сбросить пароль пользователю с ролью `super_admin` (включая самого себя) → `403 {"error":"cannot_reset_super_admin"}`. Иначе учётка админа временно попадала бы в состояние `password_reset_required=true` и была бы claimable (любой, кто знает логин, задал бы пароль на `/set-password`) до перезапуска сервиса, где `seed_admin` восстанавливает hash. Пароль админа меняется только через `ADMIN_PASSWORD` + рестарт (`seed_admin`, см. [08-security.md](./08-security.md) §1). Аналог `cannot_delete_super_admin` у `DELETE`.
- **Ответы:** `200 {"ok": true}`; `403 {"error":"cannot_reset_super_admin"}`; `404 {"error":"user_not_found"}`.

### `DELETE /api/admin/users/{id}`
- **Логика:** `delete_user` (одна транзакция, deferred constraints):
  - нельзя удалить `super_admin` → `403 {"error":"cannot_delete_super_admin"}`;
  - пользователь — лидер команды, где есть **другие** участники → `409 {"error":"user_is_leader"}` (сначала переназначить лидера через `PATCH /api/admin/teams/{id}/leader`);
  - пользователь — лидер и **единственный** участник команды → допускается: транзакция сначала `teams.leader_user_id = NULL` (команда становится пустой/orphan), затем удаление пользователя. Команда сохраняется пустой (её можно удалить отдельно);
  - обычный участник / без команды → удаление напрямую.
  - Каскадом удаляются `telegram_links`, `deliveries`. Audit `delete_user` (snapshot `target_username`); при обнулении лидера — доп. audit `team_leader_set` (`{leader_user_id: null}`).
- **Ответы:** `200 {"ok": true}`; `409 {"error":"user_is_leader"}`; `403 {"error":"cannot_delete_super_admin"}`; `404`.

### `PATCH /api/admin/users/{id}`
- **Тело (JSON):** `{"team_id": int, "display_name": str|null}` (частично — можно прислать одно из полей). Для `group_member`/`group_leader` **`team_id` не может стать `null`**: перемещение допускается только в другую **существующую** команду (CHECK `users_role_team_invariant` запрещает teamless member/leader). «Убрать из команды без перевода» — только через удаление пользователя или расформирование команды (§5), где `team_id` обнуляется атомарно вместе со сменой лидерства.
- **Логика:** смена `display_name` и/или перемещение в другую команду (`user_team_change`) — одна транзакция, deferred constraints, с соблюдением инварианта роли↔team ([04-data-model.md](./04-data-model.md)) и правила «первый=лидер» в целевой команде. Перемещение по роли исходного пользователя:
  - **лидер команды-источника, где есть другие участники** → `409 {"error":"leader_move_forbidden"}` (сначала переназначить лидера через `PATCH /api/admin/teams/{id}/leader`, иначе команда-источник осталась бы без лидера при наличии участников);
  - **лидер и единственный участник команды-источника** → допускается: транзакция обнуляет `teams.leader_user_id` источника (команда-источник становится пустой/orphan), переносит пользователя в целевую команду, применяет «первый=лидер» в целевой (если она была пустой — пользователь становится её лидером, иначе `group_member`); audit `team_leader_set` для источника;
  - **обычный участник** → перенос; применяется «первый=лидер» в целевой команде.
  - Перемещение `super_admin` в команду запрещено (нарушает инвариант) → `400 {"error":"role_team_invariant"}`.
- **Ответы:** `200 {...}`; `409 {"error":"leader_move_forbidden"}`; `400 {"error":"team_required"}` (попытка обнулить `team_id` у member/leader); `400 {"error":"role_team_invariant"}` (перемещение `super_admin` в команду); `404 {"error":"user_not_found"}` / `404 {"error":"team_not_found"}`.

---

## 5. Admin — команды

`require_admin` для CRUD команд.

### `GET /api/admin/teams`
- **Ответ `200`:** `{"teams": [{"id","name","leader_user_id","leader_username","members_count","numbers_count","is_active","created_at"}]}`.

### `POST /api/admin/teams`
- **Тело (JSON):** `{"name": str}`.
- **Логика:** `teams_service.create` — создаёт команду с `leader_user_id=NULL` (orphan до первого участника). Audit `team_create`.
- **Ответы:** `201 {"id","name"}`; `409 {"error":"team_name_taken"}`; `400 {"error":"invalid_name"}`.

### `PATCH /api/admin/teams/{id}`
- **Тело (JSON):** `{"name": str}`. `rename`. Audit `team_rename`.
- **Ответы:** `200`; `409 {"error":"team_name_taken"}`; `404`.

### `PATCH /api/admin/teams/{id}/leader`
- **Тело (JSON):** `{"new_leader_user_id": int}`.
- **Логика:** `teams_service.set_leader` — переназначение лидера **внутри** команды (одна транзакция, deferred constraints). `new_leader_user_id` обязан быть участником этой команды (`users.team_id = id`). Транзакция: текущий лидер → `role='group_member'` (остаётся участником, `team_id` не меняется); новый → `role='group_leader'`; `teams.leader_user_id = new_leader_user_id`. Разрешает выход из дедлока «лидер не может покинуть/быть удалённым, пока команда не пуста». Audit `team_leader_set` (`{previous_leader_user_id, new_leader_user_id}`).
- **Ответы:** `200 {"id","leader_user_id"}`; `400 {"error":"user_not_in_team"}` (кандидат не участник команды); `404 {"error":"team_not_found"}` / `404 {"error":"user_not_found"}`.

### `DELETE /api/admin/teams/{id}`
- **Логика:** удаление (расформирование) команды. Требует, чтобы команда была **полностью пуста**: `leader_user_id IS NULL` И нет пользователей с `team_id = id`. Иначе `409 {"error":"team_has_members"}`. Каскадом удаляются `phone_numbers` команды. `ON DELETE SET NULL` на `users.team_id` при штатном flow не срабатывает (команда уже пуста) — служит только safety-net; CHECK-инвариант роли↔team не нарушается. Audit `team_delete`.
- **Ответы:** `200 {"ok": true}`; `409 {"error":"team_has_members"}`; `404`.

### Порядок расформирования команды (disband)

Из-за инварианта роли↔team ([04-data-model.md](./04-data-model.md)) команда с участниками всегда имеет лидера и не бывает «пустой» при живом лидере. Штатный порядок расформирования:

1. Перевести/удалить всех **обычных** участников: `PATCH /api/admin/users/{id}` (в другую команду) или `DELETE /api/admin/users/{id}`.
2. Убрать лидера. Когда лидер остаётся единственным участником — один из вариантов:
   - `PATCH /api/admin/users/{leader}` с `team_id` другой команды → источник обнуляет `leader_user_id`, становится пустым;
   - `DELETE /api/admin/users/{leader}` → транзакция обнуляет `leader_user_id` и удаляет пользователя;
   - если лидера нужно сохранить в команде, а расформировать всё равно — сначала перевести остальных, лидер уходит последним по одному из способов выше.
   (Пока в команде есть **другие** участники, лидера нельзя ни перенести — `409 leader_move_forbidden`, ни удалить — `409 user_is_leader`; сначала `PATCH .../leader` переназначает лидерство.)
3. `DELETE /api/admin/teams/{id}` — удалить теперь пустую команду.

**Правило «первый=лидер»** (реализация — `teams_service.set_leader_if_absent`, срабатывает в `POST /api/admin/users` с `team_id` и `PATCH /api/admin/users/{id}`): при добавлении первого участника в команду с `leader_user_id IS NULL` — участник получает `role='group_leader'`, `teams.leader_user_id=его id`. Audit `team_leader_set`.

---

## 6. Numbers — номера команды

Источник — [ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md). **Любой участник команды** может добавлять/удалять номера своей команды.

### `GET /api/numbers`
- **Guard:** `require_authenticated`.
- **Логика:** super_admin видит все номера (опц. фильтр `?team_id=`); участник — только номера своей команды (`VisibilityScope.team_id`).
- **Ответ `200`:** `{"numbers": [{"id","phone_number","team_id","team_name","label","is_active","added_by_user_id","created_at"}]}`.

### `POST /api/numbers`
- **Guard:** `require_authenticated`.
- **Тело (JSON):** `{"phone_number": str, "label": str|null, "team_id": int|null}`.
- **Логика:** `team_id` — из `current_user.team_id` для участника/лидера; `super_admin` обязан передать `team_id` явно. Нормализация номера (E.164). `added_by_user_id = current_user.id`. Audit `number_added`.
- **Ответы:** `201 {"id","phone_number","team_id"}`; `409 {"error":"phone_number_taken"}` (номер уже привязан); `400 {"error":"invalid_phone_number"}`; `400 {"error":"team_required"}` (super_admin без team_id); `403 {"error":"forbidden"}` (участник указал чужую команду).

### `DELETE /api/numbers/{id}`
- **Guard:** `require_authenticated`.
- **Логика:** участник может удалить только номер своей команды; super_admin — любой. Audit `number_removed`.
- **Ответы:** `200 {"ok": true}`; `403 {"error":"forbidden"}`; `404 {"error":"number_not_found"}`.

---

## 7. Admin UI (SSR)

### `GET /admin`
- **Guard:** `require_admin`. Jinja2-страница: список пользователей, форма создания, кнопки reset/delete, назначение команды. Использует `admin_users.js` + `csrf.js`.

### `GET /admin/teams`
- **Guard:** `require_admin`. Jinja2-страница: список команд, создание/переименование/удаление, состав и лидер.

Обе страницы наследуют `base.html` (подключён `telegram-web-app.js` + `tg.js`, CSP `script-src 'self' https://telegram.org`).

---

## 8. System

### `GET /health`
- **Доступ:** публичный. **Ответ `200`:** `{"status": "ok", "service": "<SERVICE_NAME>"}`. Используется docker healthcheck и smoke.

---

## Сводная таблица прав

| Endpoint | Метод | Guard | CSRF |
| --- | --- | --- | --- |
| `/api/webhooks/twilio/sms` | POST | публичный (подпись Twilio) | exempt |
| `/login`, `/login/password` | GET/POST | публичный | POST: **exempt** (нет сессии/`sms_csrf`; защита — rate-limit) |
| `/set-password` | GET/POST | setup-сессия (`sms_setup`) | POST: да (есть `sms_setup`+CSRF) |
| `/logout` | POST | authenticated | да |
| `/api/telegram/auth` | POST | публичный (HMAC) | exempt |
| `/api/admin/users*` | GET/POST/PATCH/DELETE | `require_admin` | да |
| `/api/admin/teams*` | GET/POST/PATCH/DELETE | `require_admin` | да |
| `/api/admin/teams/{id}/leader` | PATCH | `require_admin` | да |
| `/api/numbers*` | GET/POST/DELETE | `require_authenticated` | да |
| `/admin`, `/admin/teams` | GET | `require_admin` | — |
| `/health` | GET | публичный | — |

## Сводная таблица ключевых кодов ошибок

| Код | HTTP | Endpoint(ы) | Смысл |
| --- | --- | --- | --- |
| `invalid_twilio_signature` | 401 | webhook | Неверная подпись Twilio. |
| `invalid_init_data` / `init_data_expired` | 401 | `/api/telegram/auth` | Плохой HMAC / протух `auth_date`. |
| `invalid_credentials` | 200/re-render | `/login/password` | Неверный логин/пароль (без различения). |
| `account_locked` | 423 | `/login/password` | Lockout до `lockout_until`. |
| `team_required` | 400 | `POST/PATCH /api/admin/users`, `POST /api/numbers` | Для member/leader обязателен `team_id`; super_admin обязан указать команду для номера. |
| `role_team_invariant` | 400 | `PATCH /api/admin/users` | Перемещение `super_admin` в команду. |
| `username_taken` | 409 | `POST /api/admin/users` | Логин занят. |
| `cannot_delete_super_admin` | 403 | `DELETE /api/admin/users/{id}` | Нельзя удалить super_admin. |
| `cannot_reset_super_admin` | 403 | `POST /api/admin/users/{id}/reset` | Нельзя сбросить пароль super_admin (защита от временного claim учётки; см. [08-security.md](./08-security.md) §1, [100-known-tech-debt.md](./100-known-tech-debt.md) TD-010). |
| `user_is_leader` / `leader_move_forbidden` | 409 | `DELETE`/`PATCH /api/admin/users/{id}` | Лидер команды с другими участниками — сначала переназначить лидера. |
| `team_has_members` | 409 | `DELETE /api/admin/teams/{id}` | Команда не пуста. |
| `user_not_in_team` | 400 | `PATCH /api/admin/teams/{id}/leader` | Кандидат в лидеры — не участник команды. |
| `phone_number_taken` | 409 | `POST /api/numbers` | Номер уже привязан. |
| `forbidden` | 403 | `/api/numbers*` | Участник обращается к чужой команде. |
