# 05. API Contracts

Все имена таблиц/полей/ролей согласованы с [04-data-model.md](./04-data-model.md); безопасность — [08-security.md](./08-security.md).

## Соглашения

- **Cookies:** `sms_session` (сессия, HttpOnly), `sms_csrf` (double-submit, читается JS), `sms_setup` (setup-сессия при первом входе), `sms_login` (промежуточная сессия шага-1 логина), `sms_tg_pending` (pending-токен Mini App SSO, HttpOnly, короткоживущий), `sms_logged_out` (маркер «залипающего» выхода, **не HttpOnly** — читается `tg.js`; подавляет авто-SSO до явного входа, [ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)).
- **CSRF:** double-submit применяется к небезопасным методам (`POST/DELETE/PATCH`) **только когда у запроса уже есть cookie с CSRF-токеном** (`sms_csrf` при аутентифицированной `sms_session`, или `sms_setup`+CSRF на `/set-password`). Проверяется совпадение заголовка/поля `csrf_token` с cookie. **Exempt** (double-submit невозможен или не нужен): `POST /api/webhooks/twilio/sms` (подпись Twilio), `POST /api/telegram/auth` (HMAC initData), `POST /api/telegram/webhook` (секрет-токен `X-Telegram-Bot-Api-Secret-Token`, [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md)), а также **`POST /login` и `POST /login/password`** — на этих шагах сессии и cookie `sms_csrf` ещё нет, поэтому CSRF-токен физически невозможен; их защищает rate-limit (`LIMIT_LOGIN` per IP / `LIMIT_LOGIN_USERNAME` per username, см. [08-security.md](./08-security.md) §4). Как только устанавливается setup-сессия (`/set-password`) или полноценная сессия (admin/numbers/logout) — CSRF обязателен.
- **Ошибки API (`/api/*`):** JSON `{"error": "<code>", "detail": "<человекочит.>"}` + соответствующий HTTP-код. Ошибки SSR-страниц (`/login`, `/admin`) — редирект/ре-рендер с флеш-сообщением.
- **Guards (`app/api/deps.py`):**
  - `require_authenticated` — есть валидная `sms_session`.
  - `require_admin` — `role == super_admin`.
  - `require_admin_or_leader` — `role IN (super_admin, group_leader)`.
  - `VisibilityScope{user_id, role, team_id, team_ids}` — область видимости. `team_id` — **домашняя** команда (для записи/дефолта); `team_ids: frozenset[int]` — **все** команды участника (home ∪ доп. членства, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)). super_admin видит всё (`team_ids` пуст); остальные — свои команды (`team_ids`).
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

Источник — [ADR-0002](./adr/ADR-0002-two-step-login.md) + его **амендмент** (2026-07-02: ветвление шага-1 по состоянию аккаунта). Мягкая анти-энумерация: несуществующий логин отвечает так же, как **активный** аккаунт с паролем (`303 → /login/password`); различимо только состояние первичной активации (`password_reset_required` → `/set-password`) — принятый риск [TD-010](./100-known-tech-debt.md).

> **Пост-логин редирект.** Все `303 → /` ниже ведут на корневой диспетчер `GET /` (§7), который редиректит на landing по роли (`/admin` для super_admin, `/app` для участников). `/` — реальный зарегистрированный маршрут; `SAFE_REDIRECT_AFTER_LOGIN` и цели пост-set-password/SSO указывают на существующую и достижимую роли страницу ([ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md)).

### `GET /login`
- SSR-форма шага-1 (ввод логина). Если Mini App прислал `sms_tg_pending` — cookie уже установлен `POST /api/telegram/auth`.
- **Маркер выхода ([ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)):** если присутствует cookie `sms_logged_out`, `tg.js` **не** выполняет авто-SSO (не POSTит initData). Страница показывает сообщение «Вы вышли из системы» и кнопку **«Войти»**: по клику `tg.js` удаляет cookie `sms_logged_out` и инициирует вход — в Mini App POSTит `init_data` в `/api/telegram/auth` (нормальное ветвление [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)), в браузере — переход к форме шага-1. Без маркера — авто-SSO как раньше.

### `POST /login` (шаг 1 — логин)
- **CSRF:** exempt (сессии/`sms_csrf` ещё нет; защита — rate-limit).
- **Тело (form):** `username`.
- **Логика (амендмент [ADR-0002](./adr/ADR-0002-two-step-login.md)):** нормализует username (lower), `lookup_for_login`, **ветвление по состоянию аккаунта**:
  - `password_hash IS NULL` **или** `password_reset_required=true` → создать setup-сессию + `Set-Cookie sms_setup` (+ парный CSRF), `303 → /set-password` («придумайте пароль» — ТЗ-флоу первого входа);
  - активный аккаунт с паролем → `303 → /login/password` (+ Set-Cookie `sms_login`);
  - логин **не существует** → `303 → /login/password` (+ Set-Cookie `sms_login`) — мягкая анти-энумерация (неотличимо от активного аккаунта; шаг-2 даст `invalid_credentials` через dummy-hash).
- **Ответы:** `303 → /set-password` (Set-Cookie `sms_setup`) **или** `303 → /login/password` (Set-Cookie `sms_login`) — по состоянию. При превышении rate-limit — `429`.

### `GET /login/password`
- SSR-форма шага-2 (ввод пароля). Требует `sms_login`; иначе `302 → /login`.

### `POST /login/password` (шаг 2 — пароль)
- **CSRF:** exempt (при `sms_login` ещё нет `sms_csrf`; защита — rate-limit).
- **Тело (form):** `password`.
- **Логика:** `login()` — argon2 verify с анти-timing dummy-hash для несуществующего/без пароля; учёт `failed_login_attempts`/`lockout_until`; `needs_rehash` → перехеширование. При `password_reset_required=true` или `password_hash IS NULL` → направление на `/set-password` (**fallback** — основной путь такого аккаунта теперь через шаг-1, см. `POST /login`; ветка сохраняется как страховка, [ADR-0002](./adr/ADR-0002-two-step-login.md) амендмент). При успехе — создание сессии `sms_session`/`sms_csrf`, `link_pending` если есть `sms_tg_pending`.
- **Ответы:**
  - `303 → /` — успех (Set-Cookie `sms_session`, `sms_csrf`; clear `sms_login`; при наличии — clear `sms_logged_out` через `Max-Age=0`, [ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)).
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
- **Ответы:** `303 → /` (Set-Cookie `sms_session`,`sms_csrf`; clear `sms_setup`; при наличии — clear `sms_logged_out` через `Max-Age=0`); `200`/re-render при слабом/несовпадающем пароле (`weak_password`/`password_mismatch`); `429` — rate-limit.

### `POST /logout`
- **Тело (form):** `csrf_token`.
- **Логика:** revoke `sms_session` (Redis), clear cookies (`sms_session`/`sms_csrf`). **`telegram_links` НЕ трогаются** (push переживает logout — см. [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)). **Ставит маркер «залипающего» выхода** `Set-Cookie sms_logged_out=1` (не HttpOnly, `Secure` при `COOKIE_SECURE`, `SameSite=Lax`, `Path=/`, TTL `LOGOUT_STICKY_TTL_SECONDS`) — подавляет авто-SSO до явного входа ([ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)). Для `super_admin` — audit `admin_logout`.
- **Ответы:** `302 → /login` (+ Set-Cookie `sms_logged_out`).

---

## 3. Telegram Mini App SSO

### `POST /api/telegram/auth`
- **Доступ:** публичный. **CSRF:** exempt (защита — HMAC initData). Источник — [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md).
- **Content-Type:** `application/json`. **Тело:** `{"init_data": "<raw initData>"}`.
- **Rate-limit:** `TG_AUTH` per IP до HMAC; per `telegram_user_id` после HMAC (см. [08-security.md](./08-security.md)).
- **Логика:** `verify_init_data` (HMAC-SHA256 + `auth_date` TTL) — до ветвления. Затем **проверка маркера выхода** ([ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md)): если есть cookie `sms_logged_out` **и нет** активной `sms_session` → **не создавать сессию/pending** → `200 {"linked": false, "logged_out": true}` (без Set-Cookie `sms_session`/`sms_tg_pending`). Иначе ветвление:
  - **есть активная `sms_session`** → `self_heal_link` (idempotent upsert привязки к текущему user; NO-OP для живой привязки того же user) → `200 {"linked": false, "healed": true}` (без redirect, без сессионной cookie). Если при этом присутствует stale-`sms_logged_out` — очистить его (`Set-Cookie sms_logged_out=; Max-Age=0`).
  - **нет сессии, привязка существует** (`telegram_links` с `dead_at IS NULL`) → создать сессию → `200 {"linked": true, "redirect": "/"}` + Set-Cookie `sms_session`,`sms_csrf`. Фронт переходит на `/`, который диспетчеризует на landing по роли (§7, [ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md)).
  - **нет сессии, привязки нет** → `create_pending` (Redis) → `200 {"linked": false}` + Set-Cookie `sms_tg_pending`.
- **Ответы ошибок:** `401 {"error":"invalid_init_data"}` (плохой HMAC) / `{"error":"init_data_expired"}` (протух `auth_date`); `429` — rate-limit; при внутренней ошибке self-heal — `200 {"linked": false, "healed": false}` (best-effort, фронт не перезагружается).

---

## 3a. Telegram webhook (приём апдейтов бота)

Источник — [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md). Заменяет удалённый long polling ([ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md)). Бот обрабатывает **только `/start`**; весь функционал — в Mini App.

### `POST /api/telegram/webhook`
- **Доступ:** публичный. **CSRF:** exempt (нет сессии; защита — секрет-токен). **Auth:** заголовок `X-Telegram-Bot-Api-Secret-Token`.
- **Content-Type:** `application/json`. **Тело:** Telegram Update (as-is).
- **Rate-limit:** per IP (см. [08-security.md](./08-security.md) §4).
- **Валидация секрета:** `X-Telegram-Bot-Api-Secret-Token` обязан совпадать с `TELEGRAM_WEBHOOK_SECRET`. Несовпадение/отсутствие → `403 {"error":"invalid_webhook_secret"}` (constant-time compare), до разбора тела.
- **Логика:**
  - `message.text == "/start"` → `sendMessage(chat_id, приветствие)` с кнопкой `web_app` (reply/inline, `url = TELEGRAM_WEBAPP_URL` = `https://novirell.shop`) → `200`.
  - **любой другой апдейт** (иной текст, callback, edited_message и т.п.) → `200` без действий (no-op). Прочих команд (в т.ч. `/help`) бот не обрабатывает.
  - Ошибка `sendMessage` (сеть/Bot API) не роняет обработчик — логируется (без секретов) и возвращается `200` (Telegram не должен ретраить бесконечно).
- **Логирование:** тело апдейта и токены **не** логируются (см. [08-security.md](./08-security.md) §9, §11); допускается лог факта «/start от chat_id=<id>» без чувствительного содержимого.
- **Ответы:** `200` (обработано/no-op); `403 {"error":"invalid_webhook_secret"}`; `429` — rate-limit.

> **Настройка webhook и меню** (`setWebhook` с `secret_token`, `setMyCommands` только `/start`) — одноразовые операции деплоя, см. [07-deployment.md](./07-deployment.md). Домен Mini App в @BotFather (`novirell.shop`) — ручная предпосылка.

---

## 4. Admin — пользователи

Все — `require_admin`. Scope-guard: super_admin действует над любыми пользователями.

### `GET /api/admin/users`
- **Ответ `200`:** `{"users": [{"id","username","display_name","role","team_id","team_name","is_leader","team_ids","password_reset_required","has_telegram_link","created_at","last_login_at"}]}`.
- **`is_leader`** — `true`, если пользователь является лидером своей **домашней** команды (`teams.leader_user_id = users.id`); используется для пометки лидера в UI-группировке.
- **`team_ids`** ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)) — список **всех** команд пользователя (home + доп. членства) из `user_teams`, для отображения пользователя в нескольких секциях на `/admin` и рендера контролов членства. `team_id` — по-прежнему домашняя команда. super_admin → `team_ids: []`.
- **Сортировка (нормативно, для группировки на `/admin`):** записи упорядочены так, чтобы клиент/SSR мог сгруппировать без доп. запросов — сначала `super_admin` (сортировка по `username`), затем пользователи по **домашней** команде (`team_name ASC`, `team_id ASC` для стабильности); внутри команды — лидер первым (`is_leader DESC`), затем участники по `username ASC`. Порядок — контракт (QA проверяет). Доп. членства (`team_ids`) выводятся из `user_teams` и раскладываются по секциям на этапе SSR-группировки (см. §7): пользователь показывается в каждой своей команде, home помечен.

### `POST /api/admin/users`
- **Тело (JSON):** `{"username": str, "display_name": str|null, "team_id": int}`. **`team_id` обязателен** — создаваемый пользователь получает роль `group_member`/`group_leader`, для которой CHECK `users_role_team_invariant` требует `team_id IS NOT NULL` ([04-data-model.md](./04-data-model.md)). Создать пользователя «без команды» нельзя (роль `super_admin` не создаётся через этот endpoint — он seed-only).
- **Логика:** `create_user` — `username` (lower, уникальный), `password_hash=NULL`, `password_reset_required=true`. Пользователь добавляется в команду `team_id` в **одной транзакции** (deferred constraints): срабатывает правило «первый=лидер» (см. §5) — если команда была пустой (`leader_user_id IS NULL`), пользователь становится `group_leader` (`teams_service.create_for_leader`/`set_leader_if_absent`), иначе `group_member`. `team_id` проставляется атомарно, поэтому CHECK-инвариант не нарушается. **Home-членство зеркалируется** ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)): в той же транзакции вставляется строка `user_teams(user_id, team_id)`. Audit `create_user`.
- **Ответы:** `201 {"id", "username", "role", "team_id"}`; `400 {"error":"team_required"}` (team_id отсутствует/пуст); `409 {"error":"username_taken"}`; `400 {"error":"invalid_username"}`; `404 {"error":"team_not_found"}`.

### `POST /api/admin/users/{id}/reset`
- **Логика:** `reset_password` — `password_hash=NULL`, `password_reset_required=true`; revoke всех сессий пользователя (Redis) и **всех** `telegram_links` (`reason="password_reset"`). Audit `reset_password`.
- **Запрет сброса super_admin:** нельзя сбросить пароль пользователю с ролью `super_admin` (включая самого себя) → `403 {"error":"cannot_reset_super_admin"}`. Иначе учётка админа временно попадала бы в состояние `password_reset_required=true` и была бы claimable (любой, кто знает логин, задал бы пароль на `/set-password`) до перезапуска сервиса, где `seed_admin` восстанавливает hash. Пароль админа меняется только через `ADMIN_PASSWORD` + рестарт (`seed_admin`, см. [08-security.md](./08-security.md) §1). Аналог `cannot_delete_super_admin` у `DELETE`.
- **Ответы:** `200 {"ok": true}`; `403 {"error":"cannot_reset_super_admin"}`; `404 {"error":"user_not_found"}`.

### `DELETE /api/admin/users/{id}`
- **Логика:** `delete_user` (одна транзакция, deferred constraints):
  - нельзя удалить `super_admin` → `403 {"error":"cannot_delete_super_admin"}`;
  - пользователь — лидер команды, где есть **другие ДОМАШНИЕ** участники (`users.team_id = team`, `count_home_members > 1` — HOME-семантика, [03 §Multi-team](./03-architecture.md); доп.-участники с home в другой команде **НЕ** учитываются) → `409 {"error":"user_is_leader"}` (сначала переназначить лидера через `PATCH /api/admin/teams/{id}/leader`, который требует home-членства кандидата — §7);
  - пользователь — лидер и **единственный ДОМАШНИЙ** участник команды (нет других `users.team_id = team`; доп.-членства прочих пользователей не блокируют) → допускается: транзакция сначала `teams.leader_user_id = NULL` (команда становится пустой/orphan по home), затем удаление пользователя. Команда сохраняется пустой (её можно удалить отдельно);
  - обычный участник / без команды → удаление напрямую.
  - Каскадом удаляются `telegram_links`, `deliveries`. Audit `delete_user` (snapshot `target_username`); при обнулении лидера — доп. audit `team_leader_set` (`{leader_user_id: null}`).
- **Ответы:** `200 {"ok": true}`; `409 {"error":"user_is_leader"}`; `403 {"error":"cannot_delete_super_admin"}`; `404`.

### `PATCH /api/admin/users/{id}`
- **Тело (JSON):** `{"team_id": int, "display_name": str|null}` (частично — можно прислать одно из полей). Для `group_member`/`group_leader` **`team_id` не может стать `null`**: перемещение допускается только в другую **существующую** команду (CHECK `users_role_team_invariant` запрещает teamless member/leader). «Убрать из команды без перевода» — только через удаление пользователя или расформирование команды (§5), где `team_id` обнуляется атомарно вместе со сменой лидерства.
- **Логика:** смена `display_name` и/или перемещение в другую **домашнюю** команду (`user_team_change`) — одна транзакция, deferred constraints, с соблюдением инварианта роли↔team ([04-data-model.md](./04-data-model.md)) и правила «первый=лидер» в целевой команде. **Синхронизация `user_teams`** ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)): move меняет **домашнюю** команду — в той же транзакции **удаляется старая home-строка** (`team_id=старый users.team_id`) и **добавляется новая home-строка** (`team_id=новый`); доп. членства не трогаются. **Ревокация сессий** target ([ADR-0012](./adr/ADR-0012-multi-team-membership.md) §6). Перемещение по роли исходного пользователя:
  - **лидер команды-источника, где есть другие ДОМАШНИЕ участники** (`users.team_id = источник`, `count_home_members > 1` — HOME-семантика, [03 §Multi-team](./03-architecture.md); доп.-участники с home в другой команде **НЕ** учитываются) → `409 {"error":"leader_move_forbidden"}` (сначала переназначить лидера через `PATCH /api/admin/teams/{id}/leader`, иначе команда-источник осталась бы без лидера при наличии home-участников);
  - **лидер и единственный ДОМАШНИЙ участник команды-источника** (нет других `users.team_id = источник`; доп.-членства прочих не блокируют) → допускается: транзакция обнуляет `teams.leader_user_id` источника (команда-источник становится пустой/orphan по home), переносит пользователя в целевую команду, применяет «первый=лидер» в целевой (если она была пустой — пользователь становится её лидером, иначе `group_member`); audit `team_leader_set` для источника;
  - **обычный участник** → перенос; применяется «первый=лидер» в целевой команде.
  - Перемещение `super_admin` в команду запрещено (нарушает инвариант) → `400 {"error":"role_team_invariant"}`.
- **Ответы:** `200 {...}`; `409 {"error":"leader_move_forbidden"}`; `400 {"error":"team_required"}` (попытка обнулить `team_id` у member/leader); `400 {"error":"role_team_invariant"}` (перемещение `super_admin` в команду); `404 {"error":"user_not_found"}` / `404 {"error":"team_not_found"}`.

### `POST /api/admin/users/{id}/teams` (добавить доп. членство)

Источник — [ADR-0012](./adr/ADR-0012-multi-team-membership.md). `require_admin` (super_admin-only). Добавляет пользователя в **дополнительную** команду, не меняя домашнюю (`users.team_id`) и роль.
- **Тело (JSON):** `{"team_id": int}`.
- **Логика:** `UserTeamRepository.add` — идемпотентный `INSERT ... ON CONFLICT (user_id, team_id) DO NOTHING`; при уже существующем членстве → `409`. Роль/лидерство не меняются (доп. членство не делает лидером). **Ревокация сессий** target ([ADR-0012](./adr/ADR-0012-multi-team-membership.md) §6). Audit `user_team_add` (`{team_id}`).
- **Ответы:**
  - `201 {"user_id","team_id"}` — членство создано;
  - `400 {"error":"cannot_add_super_admin_to_team"}` — target имеет роль `super_admin` (членства для него запрещены);
  - `404 {"error":"user_not_found"}` / `404 {"error":"team_not_found"}`;
  - `409 {"error":"membership_already_exists"}` — пользователь уже в этой команде (в т.ч. если это его домашняя команда).

### `DELETE /api/admin/users/{id}/teams/{team_id}` (убрать доп. членство)

Источник — [ADR-0012](./adr/ADR-0012-multi-team-membership.md). `require_admin`. Убирает **дополнительное** членство. Домашнее членство удалить нельзя (смена домашней — через `PATCH` move).
- **Логика:** `UserTeamRepository.remove`. Если `team_id == users.team_id` (домашняя) → `400 cannot_remove_home_membership`. **Ревокация сессий** target. Audit `user_team_remove` (`{team_id}`).
- **Ответы:**
  - `204` — членство удалено;
  - `400 {"error":"cannot_remove_home_membership"}` — попытка удалить домашнюю команду;
  - `404 {"error":"membership_not_found"}` — такого членства нет;
  - `404 {"error":"user_not_found"}`.
- **Form-fallback (no-JS):** `POST` на **тот же путь ресурса** `/api/admin/users/{id}/teams/{team_id}` с полем `_method=DELETE` (паттерн проекта MethodOverride, как у `/api/numbers/{id}`) → эквивалентно `DELETE`. Отдельного `.../delete`-суффикса **нет** — override работает по same-path. Регекс whitelist `_OVERRIDE_REGEX_PATHS`: `^/api/admin/users/\d+/teams/\d+$`. CSRF double-submit применяется.

---

## 4a. Admin — номера (unassigned-пул и распределение)

Источник — [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md). Все — `require_admin` (только super_admin). Распределение произвольной команды — привилегия админа; участник управляет только своей командой через §6.

### `GET /api/admin/numbers`
- **Guard:** `require_admin`.
- **Query:** `?assignment=assigned|unassigned|all` (по умолчанию `all`); `?team_id=<id>` (фильтр по конкретной команде).
- **Логика:** список **всех** номеров. `unassigned` → `team_id IS NULL`; `assigned` → `team_id IS NOT NULL`; `team_id=<id>` → номера этой команды.
- **Конфликт фильтров:** `team_id=<id>` вместе с `assignment=unassigned` логически несовместимы (конкретная команда vs «нет команды») → `400 {"error":"invalid_query"}`. Комбинация `team_id=<id>` + `assignment=assigned` (или `all`) допустима и эквивалентна фильтру по команде.
- **Ответ `200`:** `{"numbers": [{"id","phone_number","team_id","team_name","label","is_active","added_by_user_id","created_at"}]}`. Для unassigned — `team_id=null`, `team_name=null`.

### `PATCH /api/admin/numbers/{id}`
- **Guard:** `require_admin`. **Тело (JSON):** `{"team_id": <int>|null}`.
- **Логика:** назначение/переназначение/снятие команды у номера. `team_id=<id>` — привязать к существующей команде; `team_id=null` — снять (сделать unassigned). Audit `number_team_assigned` (`{number_id, phone_number, previous_team_id, new_team_id}`).
- **Ответы:**
  - `200 {"id","phone_number","team_id","team_name"}` (при `team_id=null` → `team_name=null`);
  - `404 {"error":"number_not_found"}`;
  - `404 {"error":"team_not_found"}` (переданный `team_id` не существует).

### `POST /api/admin/numbers/sync`
Источник — [ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md). On-demand подтягивание входящих номеров Twilio-аккаунта в `phone_numbers` как **unassigned**.
- **Guard:** `require_admin` (только super_admin). **CSRF:** да (double-submit; есть сессия/`sms_csrf`). **Тело:** пустое.
- **Логика:** через Twilio API (аутентификация `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` из конфига — те же секреты, что для подписи вебхука) получить **все** входящие номера аккаунта, пройдя **все страницы** (пагинация). Каждый E.164-номер нормализуется `normalize_phone`, upsert в `phone_numbers` как unassigned (`team_id = NULL`, `added_by_user_id = NULL`) идемпотентно `INSERT ... ON CONFLICT (phone_number) DO NOTHING`. Существующие номера (в т.ч. **назначенные командам**) НЕ трогаются — ни `team_id`, ни `label`, ни `added_by_user_id`. Авто-назначения нет: распределение по командам — по-прежнему через `PATCH /api/admin/numbers/{id}`. Twilio SDK синхронный — вызов из async-хендлера через threadpool/executor (реализация — на усмотрение backend, сверить с кодом проекта). Audit `numbers_synced` (`details = {synced_total, added, skipped_existing}`).
- **Ответы:**
  - `200 {"synced_total": <int>, "added": <int>, "skipped_existing": <int>}` — `synced_total` = получено номеров из Twilio (по всем страницам); `added` = реально вставлено новых; `skipped_existing` = `synced_total − added` (уже присутствовали).
  - `502`/`503 {"error":"twilio_error"}` — сбой Twilio API (сеть, 5xx от Twilio, таймаут, ошибка аутентификации). Частичный прогресс не создаёт дублей (каждая вставка идемпотентна; повтор дособерёт).
  - `503 {"error":"twilio_not_configured"}` — `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` не сконфигурированы.

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
- **Логика:** удаление (расформирование) команды. **Gate роспуска = home-пустота (нормативно):** предусловие «пустая команда» проверяется **только по домашним участникам** — `leader_user_id IS NULL` И нет пользователей с `users.team_id = id`. Проверка **НЕ** смотрит в `user_teams`: доп.-участники (чья домашняя команда — другая, а эта — лишь доп. членство) **НЕ блокируют** роспуск. Иначе `409 {"error":"team_has_members"}` (только при непустоте по home). **Номера команды НЕ удаляются** — через `ON DELETE SET NULL` их `team_id` обнуляется, и они возвращаются в unassigned-пул ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md); ранее — каскадное удаление). `ON DELETE SET NULL` на `users.team_id` при штатном flow не срабатывает (команда уже пуста по home) — служит только safety-net; CHECK-инвариант роли↔team не нарушается. **Доп.-членства и ревок сессий ([ADR-0012](./adr/ADR-0012-multi-team-membership.md) §6, нормативно):** оставшиеся доп.-членства снимаются `ON DELETE CASCADE` на `user_teams.team_id`. Ревокация — по **всем** членам `user_teams` этой команды: перед/в транзакции удаления собрать `list_user_ids_in_team(id)` (по `user_teams`) и вызвать `revoke_all_for_user` для **каждого** — иначе их `VisibilityScope.team_ids` хранит id удалённой команды до re-login. Итого: **gate = home-пустота, ревокация = все строки `user_teams`.** Audit `team_delete` (`details` включает число ставших unassigned номеров и число ревокнутых доп.-участников).
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

## 6. Numbers — номера команд участника

Источник — [ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md), multi-team — [ADR-0012](./adr/ADR-0012-multi-team-membership.md). **Любой участник** может добавлять/удалять номера **любой из своих команд** (`VisibilityScope.team_ids` = home ∪ доп. членства).

### `GET /api/numbers`
- **Guard:** `require_authenticated`.
- **Логика:** super_admin видит все номера (опц. фильтр `?team_id=`; может включать unassigned с `team_name=null`); участник — номера **всех своих команд** (`phone_numbers.team_id = ANY(VisibilityScope.team_ids)`, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)), а не только домашней. Для управления unassigned-пулом/распределения админ использует §4a (`GET/PATCH /api/admin/numbers`); данный endpoint остаётся общим просмотром.
- **Ответ `200`:** `{"numbers": [{"id","phone_number","team_id","team_name","label","is_active","added_by_user_id","created_at"}]}`.

### `POST /api/numbers`
- **Guard:** `require_authenticated`.
- **Тело (JSON):** `{"phone_number": str, "label": str|null, "team_id": int|null}`.
- **Логика:** для участника/лидера `team_id` обязан быть **одной из своих команд** (`∈ VisibilityScope.team_ids`, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)); если не передан — дефолт `current_user.team_id` (домашняя). `super_admin` обязан передать `team_id` явно. Нормализация номера (E.164). `added_by_user_id = current_user.id`. Audit `number_added`.
- **Ответы:** `201 {"id","phone_number","team_id"}`; `409 {"error":"phone_number_taken"}` (номер уже привязан); `400 {"error":"invalid_phone_number"}`; `400 {"error":"team_required"}` (super_admin без team_id); `403 {"error":"forbidden"}` (участник указал команду **не из своих** `team_ids`).

### `DELETE /api/numbers/{id}`
- **Guard:** `require_authenticated`.
- **Логика:** участник может удалить номер **любой своей команды** (`phone_numbers.team_id ∈ VisibilityScope.team_ids`); super_admin — любой. Audit `number_removed`.
- **Ответы:** `200 {"ok": true}`; `403 {"error":"forbidden"}` (номер не из команд участника); `404 {"error":"number_not_found"}`.

---

## 7. SSR-страницы (UI)

Источник per-role landing — [ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md).

### `GET /` — корневой диспетчер по роли
- **Guard:** нет (обрабатывает и анонима). **Контента не рендерит** — только редирект.
- **Логика:** единственная точка выбора landing. Существующие пост-логин/пост-set-password (`303 → /`) и Mini App SSO (`redirect:"/"`) ведут сюда, а `/` доводит до страницы, достижимой для роли.
- **Ответы:**
  - `302 → /login` — нет валидной `sms_session`;
  - `302 → /admin` — `role == super_admin`;
  - `302 → /app` — `role IN (group_leader, group_member)`.

### `GET /app` — landing участника/лидера (SSR)
- **Guard:** `require_authenticated`. Роли: `group_leader`, `group_member`. Для `super_admin` (нет команды) — `302 → /admin` (у него landing `/admin`, тупиков нет).
- **Логика:** SSR Jinja2-страница; scope = `VisibilityScope.team_ids` (все команды участника, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)). Сервер инжектирует в контекст: номера **всех своих команд** (тот же набор, что `GET /api/numbers` для участника), **сгруппированные по командам**; список своих команд `teams: list[{id, name}]` (для селектора команды в форме добавления); статус собственной Telegram-привязки (`has_telegram_link` — есть `telegram_links` текущего user с `dead_at IS NULL`); `display_name`/`username`; `csrf_token`. **Нового API для статуса привязки не вводится** — данные SSR-инжектятся.
- **Действия на странице:** добавить номер (`POST /api/numbers` — `team_id` берётся из **селектора** выбранной своей команды; при единственной команде — она же дефолт), удалить номер (`DELETE /api/numbers/{id}`), logout (`POST /logout`). Мутации — через существующие endpoints, CSRF double-submit (`csrf.js`/hidden-поле).
- **Права:** любой участник (member/leader) управляет номерами **любой из своих** команд ([ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md), [ADR-0012](./adr/ADR-0012-multi-team-membership.md)). Управление участниками команды лидером — вне scope этой итерации ([ADR-0003](./adr/ADR-0003-roles-and-teams.md), [ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md) §3).
- **Ответ:** `200` (SSR HTML) для member/leader; `302 → /admin` для super_admin; `302 → /login` при отсутствии сессии (через `NotAuthenticatedError handler`).

### `GET /admin`
- **Guard:** `require_admin`. Jinja2-страница: сгруппированный список пользователей, форма создания, кнопки reset/delete, назначение команды, **секция распределения unassigned-номеров**. Использует `admin_users.js` + `csrf.js`.
- **SSR-контекст (нормативно, группировка — референс `mail-agregator/backend/app/templates/admin/users.html`):** сервер **предгруппирует** данные в Python (не только сортирует), инжектирует:
  - `super_admins: list` — сначала (обычно один); поля `{id, username, display_name, role, created_at, last_login_at, has_telegram_link}`. Секция «Администраторы» рендерится **первой**.
  - `team_sections: list` — секции по командам, отсортированные `team_name ASC` (стабильно по `team_id`). Каждая: `{team_id, team_name, leader_user_id, members: list}`. `members` — сначала лидер (`is_leader=true`), затем участники по `username ASC`. Поля участника: `{id, username, display_name, role, is_leader, is_home, has_telegram_link, password_reset_required, created_at, last_login_at}`. Пометка лидера — явная (`is_leader`/бейдж). **Banding-класс НЕ входит в контракт** — вычисляется в шаблоне (см. ниже).
  - **Multi-team ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)):** пользователь, состоящий в нескольких командах (`team_ids`), попадает **в каждую** свою `team_section`. В домашней команде `is_home=true` (помечен бейджем «домашняя»); в доп. командах `is_home=false`. Лидер помечается `is_leader=true` только в своей домашней команде. Группировка строится из bulk-членств (`UserTeamRepository.list_team_ids_for_users`), а не только из `users.team_id`. На каждом участнике — контролы членства: «Добавить в команду» (`POST .../teams`) и «Убрать из команды» для доп. членств (`DELETE .../teams/{team_id}`; home-членство контрола удаления не имеет — `cannot_remove_home_membership`).
  - **Banding (подсветка команд, [03-architecture.md](./03-architecture.md) §«Подсветка команд»):** чисто **презентационный** слой — backend **не** отдаёт banding-поле. Класс банда вычисляется **в шаблоне** `admin/users.html` чередованием по секциям `team_sections` (namespace-счётчик: `admin__group--band-a` / `--band-b`); секция «Администраторы» — нейтральный `admin__group--no-team`. CSP-safe (только CSS-классы, без inline-style); схема БД не меняется. Порядок секций (`team_name ASC`) задаёт детерминированное чередование.
  - `teams: list[{id, name}]` — все команды (для select при создании/перемещении/добавлении членства); может быть пустым.
  - `csrf_token`, `is_super_admin: true`.
  - Данные согласованы с `GET /api/admin/users` (§4, те же поля + `team_ids` + порядок); группировка — представление того же набора. Отдельного grouping-API не вводится — SSR-инжект.
- **Секция unassigned-номеров ([ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md), [ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md)):** сервер инжектирует `unassigned_numbers: list[{id, phone_number, label, is_active, created_at}]` (номера с `team_id IS NULL`) и `teams` (для выбора команды). На каждый номер — контрол назначения команды (select команды + submit), мутация через `PATCH /api/admin/numbers/{id}` `{team_id}` (§4a, CSRF double-submit, `_method=PATCH` для no-JS). Каноничный источник данных — `GET /api/admin/numbers?assignment=unassigned`; SSR-инжект — первичный рендер.
  - **Кнопка «Синхронизировать из Twilio» ([ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md)):** в этой же секции — контрол, вызывающий `POST /api/admin/numbers/sync` (CSRF double-submit). После ответа `200 {synced_total, added, skipped_existing}` — показать результат (например «добавлено N, пропущено M») и **обновить список unassigned** (перезагрузка страницы/секции либо перезапрос `GET /api/admin/numbers?assignment=unassigned`). Ошибки `twilio_error`/`twilio_not_configured` (§4a) — показать флеш/сообщение, список не менять. Способ вызова (JS-fetch через `csrf.js` vs POST-форма) — на усмотрение frontend (сверить с кодом проекта); контракт — наличие кнопки и её привязка к endpoint'у.

### `GET /admin/teams`
- **Guard:** `require_admin`. Jinja2-страница: список команд, создание/переименование/удаление, состав и лидер.

### `GET /messages` — просмотр входящих SMS (все роли)
- **Guard:** `require_authenticated` (super_admin, group_leader, group_member). Read-only просмотр истории `inbound_sms` с ролевым селектором номеров и cursor-пагинацией. Полный контракт (видимость, no-JS fallback, курсор) — §9. Пункт «Сообщения» в шапке `base.html` — для всех аутентифицированных.

Все SSR-страницы наследуют `base.html` (подключён `telegram-web-app.js` + `tg.js`, CSP `script-src 'self' https://telegram.org`).

### Инвариант достижимости landing (нормативно)
У **каждой** аутентифицированной роли есть landing, отдающий `200` после диспетчеризации `/`; цель любого пост-логин / пост-set-password / SSO-редиректа существует и доступна роли (см. [ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md) §4). QA проверяет рендер `200` после логина для каждой роли ([06-testing-strategy.md](./06-testing-strategy.md)).

---

## 8. System

### `GET /health`
- **Доступ:** публичный. **Ответ `200`:** `{"status": "ok", "service": "<SERVICE_NAME>"}`. Используется docker healthcheck и smoke.

---

## 9. Messages — просмотр входящих SMS ([ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md))

Read-only просмотр истории `inbound_sms`. Единая страница для всех ролей; различие — набор видимых номеров и правила фильтрации. Связь SMS↔номер — по `inbound_sms.to_number = phone_numbers.phone_number`. **Видимость участника — по ТЕКУЩЕЙ принадлежности номера (`phone_numbers.team_id`), а не по снимку `inbound_sms.team_id`** ([ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md) §2). Мутаций нет (нет прочитанности/ответа/удаления).

### Контракт cursor keyset-пагинации (нормативно; первый в проекте)

Общий для §9 (и эталон для будущих листингов). Сортировка результата — **`received_at DESC, id DESC`** (`id` — детерминированный tie-breaker).

- **`cursor`** — opaque `base64url`-токен (без padding), кодирующий **только позицию** — пару `(received_at, id)` последней отданной строки. Клиент его **не парсит и не конструирует**; передаёт как есть. Кодирование/декодирование — на сервере. Пустой/отсутствующий `cursor` → первая страница.
- **Курсор не кодирует фильтры.** `to_number` / `team_id` / `limit` клиент **пересылает** при каждом запросе рядом с `cursor`. Сервер не связывает курсор с набором фильтров (смена фильтров со старым курсором → валидная, но семантически смешанная страница — ответственность клиента; ошибкой не считается).
- **`limit`** — целое, **дефолт `50`**, диапазон **`[1, 100]`**. Вне диапазона → `400 invalid_limit`.
- **Keyset-предикат** (страница после `(r0, id0)`): `received_at < :r0 OR (received_at = :r0 AND id < :id0)`.
- **Определение следующей страницы:** сервер читает `limit + 1` строк; при `> limit` — лишняя отбрасывается, `next_cursor` = позиция limit-й (последней оставленной) строки. При `≤ limit` — `next_cursor = null` (страниц больше нет).
- **Битый/недекодируемый курсор** → `400 invalid_cursor`.
- **Forward-only:** «назад» не поддерживается; возврат к началу = запрос без `cursor`.

### Сериализация SMS (`serialize_message`)

Безопасное подмножество полей `inbound_sms`; **`raw_payload` не отдаётся** (может содержать служебные/чувствительные данные Twilio — [ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md)):

```
{"id", "from_number", "to_number", "body", "received_at", "team_id"}
```

`team_id` — **снимок** команды на момент приёма (`inbound_sms.team_id`; исторический, для видимости не используется). `received_at` — ISO 8601 с таймзоной. Все имена — сверить с моделью `shared/models/inbound_sms.py` и кодом сериализатора.

### `GET /api/messages`
- **Guard:** `require_authenticated` (все роли).
- **Query:** `to_number?: str` (E.164, точное сравнение с `inbound_sms.to_number`), `team_id?: int` (**учитывается только для super_admin**; у не-super_admin игнорируется), `cursor?: str` (opaque), `limit?: int` (дефолт 50, `[1,100]`).
- **Видимость:**
  - **super_admin** — все `inbound_sms` (включая SMS, чей `to_number` уже не сопоставлён ни с одним `phone_numbers`). `to_number` — фильтр по конкретному номеру. `team_id` — фильтр по **текущей** принадлежности номера: `to_number IN (SELECT phone_number FROM phone_numbers WHERE team_id = :team_id)`.
  - **group_member / group_leader** — только SMS номеров **своих команд** (`VisibilityScope.team_ids`, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)): `inbound_sms.to_number IN (SELECT phone_number FROM phone_numbers WHERE team_id = ANY(:scope_team_ids))`. Пустой `scope.team_ids` → пустой результат. `to_number` — сужение внутри видимого набора; если запрошенный `to_number` **вне** scope участника → **пустой результат `200`** (не `403`/`404` — анти-энумерация, [ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md)). `team_id` игнорируется.
- **Ответ `200`:** `{"messages": [<serialize_message>...], "next_cursor": "<opaque>" | null}`.
- **Ошибки:** `400 {"error":"invalid_cursor"}` (битый курсор); `400 {"error":"invalid_limit"}` (`limit` вне `[1,100]`); `401` (нет сессии — через `NotAuthenticatedError handler`, JSON для `/api/*`).

### `GET /messages` — SSR-страница просмотра SMS (все роли)
- **Guard:** `require_authenticated`. Роли: **все аутентифицированные** (super_admin, group_leader, group_member). Пункт «Сообщения» в шапке (`base.html`) — для всех аутентифицированных.
- **Логика:** SSR Jinja2-страница. Сервер рендерит **первую (или запрошенную по `cursor`) страницу SMS server-side**, применяя те же правила видимости/пагинации, что `GET /api/messages` (та же сервис-функция; `to_number`/`team_id`/`cursor`/`limit` читаются из query). Ролевой селектор:
  - **super_admin** — селектор **всех** номеров (переиспользует ролевой `GET /api/numbers`, который для super_admin отдаёт все номера) + селектор команды (`teams`) для фильтра `team_id`.
  - **group_member / group_leader** — селектор **своих** номеров (тот же набор, что `GET /api/numbers` для участника). Селектора команды нет; `team_id` для участника не применяется.
- **no-JS fallback (обязателен):** фильтр — GET-форма (`method="GET" action="/messages"`) с `<select>` номера (для super_admin — ещё `<select>` команды) и submit; пагинация — обычная ссылка «Дальше» на `/messages?...&cursor=<next_cursor>` (форвардная, рендерится только когда `next_cursor != null`). Прогрессивное JS-обогащение (fetch к `GET /api/messages`) допустимо, но страница обязана работать без JS. Курсор в ссылках — тот же opaque-токен.
- **SSR-контекст (НОРМАТИВНО; split-ownership: шаблон `app/api/templates/messages.html` авторит frontend, контекст рендера передаёт backend).** Backend **ОБЯЗАН** инжектировать в шаблон ровно следующие переменные **под этими ЛИТЕРАЛЬНЫМИ именами** (не `selected_*`, не иные варианты) — шаблон читает их именно так. Канонические имена совпадают с именами query-параметров (`to_number`, `team_id`, `limit`), чтобы preselect фильтров и JS-«Ещё» работали без переименований. Список — исчерпывающий; отсутствие любой из переменных или иное имя = функциональный дефект (теряется активный фильтр, JS-пагинация теряет фильтр). Структура элементов `messages`/`numbers` — по `app/api/serializers.py`:
  - `messages: list[dict]` — сериализованные SMS текущей (или запрошенной по `cursor`) страницы через `serialize_message`; структура элемента — §9 «Сериализация SMS» (`{id, from_number, to_number, body, received_at, team_id}`, `raw_payload` не отдаётся). Порядок — `received_at DESC, id DESC`.
  - `next_cursor: str | None` — opaque-курсор следующей страницы. Ссылка «Дальше» (`/messages?...&cursor=<next_cursor>`) рендерится **только при `next_cursor != null`** (forward-only).
  - `numbers: list[dict]` — номера для `<select>` фильтра `to_number` через `serialize_number`; поля элемента: `{id, phone_number, team_id, team_name, label, is_active, added_by_user_id, created_at}` (`team_name` = `null` для unassigned). Набор: для super_admin — **все** номера; для участника — номера **его команд** (`VisibilityScope.team_ids`, [ADR-0012](./adr/ADR-0012-multi-team-membership.md)); пустой при пустом scope.
  - `teams: list[{id, name}]` — команды для `<select>` фильтра `team_id`. **Только для super_admin** (сортировка по `name`); для участника — **пустой список** (селектор команды не рендерится). Семантически это набор, отличный от `teams` в §7 (`/admin`, `/app`), — здесь фильтр по текущей принадлежности номера.
  - `is_super_admin: bool` — рендерить ли селектор команды и фильтр `team_id`.
  - `to_number: str | None` — **ТЕКУЩЕЕ** значение фильтра номера (E.164) для preselect `<select name="to_number">`, либо `None`. Имя литеральное — **не** `selected_to_number`.
  - `team_id: int | None` — **ТЕКУЩЕЕ** значение фильтра команды (для preselect `<select name="team_id">`); **только super_admin** (backend передаёт `team_id if is_super_admin else None`, поэтому у не-super_admin всегда `None`). Имя литеральное — **не** `selected_team_id`.
  - `limit: int` — текущий размер страницы (**дефолт `50`**, диапазон `[1,100]`; рендерится в скрытое поле формы `<input name="limit">` и подставляется в JS-запрос «Ещё»). **Обязателен**: backend **должен** передавать `limit` в контекст (ранее отсутствовал — без него no-JS форма и JS-пагинация теряют размер страницы).
  - `csrf_token: str`, `username: str`, `display_name: str | None` — как на прочих SSR-страницах (§7). `display_name` nullable (`users.display_name` — `Mapped[str | None]`; backend передаёт `user.display_name` напрямую, значение может быть `null`).
- Мутаций страница не выполняет (read-only) — POST-форм, кроме logout из `base.html`, нет.
- **Ответ:** `200` (SSR HTML) для всех ролей; `302 → /login` при отсутствии сессии. Битый `cursor` в query → страница показывает пустой список / сообщение (или `400`) — конкретика UX на усмотрение frontend; API-семантика `invalid_cursor` — выше.

---

## Сводная таблица прав

| Endpoint | Метод | Guard | CSRF |
| --- | --- | --- | --- |
| `/api/webhooks/twilio/sms` | POST | публичный (подпись Twilio) | exempt |
| `/login`, `/login/password` | GET/POST | публичный | POST: **exempt** (нет сессии/`sms_csrf`; защита — rate-limit) |
| `/set-password` | GET/POST | setup-сессия (`sms_setup`) | POST: да (есть `sms_setup`+CSRF) |
| `/logout` | POST | authenticated | да |
| `/api/telegram/auth` | POST | публичный (HMAC) | exempt |
| `/api/telegram/webhook` | POST | публичный (секрет-токен `X-Telegram-Bot-Api-Secret-Token`) | exempt |
| `/api/admin/users*` | GET/POST/PATCH/DELETE | `require_admin` | да |
| `/api/admin/users/{id}/teams` | POST | `require_admin` | да |
| `/api/admin/users/{id}/teams/{team_id}` | DELETE (+ POST same-path `_method=DELETE`) | `require_admin` | да |
| `/api/admin/numbers*` | GET/PATCH | `require_admin` | да |
| `/api/admin/numbers/sync` | POST | `require_admin` | да ([ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md)) |
| `/api/admin/teams*` | GET/POST/PATCH/DELETE | `require_admin` | да |
| `/api/admin/teams/{id}/leader` | PATCH | `require_admin` | да |
| `/api/numbers*` | GET/POST/DELETE | `require_authenticated` | да |
| `/api/messages` | GET | `require_authenticated` (read-only) | — (GET) |
| `/` | GET | публичный (диспетчер: 302 по роли/на `/login`) | — |
| `/app` | GET | `require_authenticated` (member/leader; super_admin → 302 `/admin`) | — |
| `/messages` | GET | `require_authenticated` (все роли) | — |
| `/admin`, `/admin/teams` | GET | `require_admin` | — |
| `/health` | GET | публичный | — |

## Сводная таблица ключевых кодов ошибок

| Код | HTTP | Endpoint(ы) | Смысл |
| --- | --- | --- | --- |
| `invalid_twilio_signature` | 401 | webhook | Неверная подпись Twilio. |
| `invalid_init_data` / `init_data_expired` | 401 | `/api/telegram/auth` | Плохой HMAC / протух `auth_date`. |
| `invalid_webhook_secret` | 403 | `/api/telegram/webhook` | Неверный/отсутствует `X-Telegram-Bot-Api-Secret-Token`. |
| `invalid_credentials` | 200/re-render | `/login/password` | Неверный логин/пароль (без различения). |
| `account_locked` | 423 | `/login/password` | Lockout до `lockout_until`. |
| `team_required` | 400 | `POST/PATCH /api/admin/users`, `POST /api/numbers` | Для member/leader обязателен `team_id`; super_admin обязан указать команду для номера. |
| `role_team_invariant` | 400 | `PATCH /api/admin/users` | Перемещение `super_admin` в команду. |
| `username_taken` | 409 | `POST /api/admin/users` | Логин занят. |
| `cannot_delete_super_admin` | 403 | `DELETE /api/admin/users/{id}` | Нельзя удалить super_admin. |
| `cannot_add_super_admin_to_team` | 400 | `POST /api/admin/users/{id}/teams` | Нельзя добавить super_admin в команду ([ADR-0012](./adr/ADR-0012-multi-team-membership.md)). |
| `membership_already_exists` | 409 | `POST /api/admin/users/{id}/teams` | Пользователь уже в этой команде. |
| `cannot_remove_home_membership` | 400 | `DELETE /api/admin/users/{id}/teams/{team_id}` | Нельзя убрать домашнюю команду (смена — через `PATCH` move). |
| `membership_not_found` | 404 | `DELETE /api/admin/users/{id}/teams/{team_id}` | Такого членства нет. |
| `cannot_reset_super_admin` | 403 | `POST /api/admin/users/{id}/reset` | Нельзя сбросить пароль super_admin (защита от временного claim учётки; см. [08-security.md](./08-security.md) §1, [100-known-tech-debt.md](./100-known-tech-debt.md) TD-010). |
| `user_is_leader` / `leader_move_forbidden` | 409 | `DELETE`/`PATCH /api/admin/users/{id}` | Лидер команды с другими **домашними** участниками (`users.team_id`, HOME-семантика; доп.-участники не блокируют) — сначала переназначить лидера. |
| `team_has_members` | 409 | `DELETE /api/admin/teams/{id}` | Команда не пуста **по home** (есть `users.team_id = id`; доп.-членства не блокируют роспуск — [ADR-0012](./adr/ADR-0012-multi-team-membership.md) §5/§6). |
| `user_not_in_team` | 400 | `PATCH /api/admin/teams/{id}/leader` | Кандидат в лидеры — не участник команды. |
| `invalid_cursor` | 400 | `GET /api/messages` | Битый/недекодируемый opaque-курсор пагинации ([ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md)). |
| `invalid_limit` | 400 | `GET /api/messages` | `limit` вне диапазона `[1,100]`. |
| `phone_number_taken` | 409 | `POST /api/numbers` | Номер уже привязан. |
| `forbidden` | 403 | `/api/numbers*` | Участник обращается к чужой команде. |
| `invalid_query` | 400 | `GET /api/admin/numbers` | Несовместимые фильтры (`team_id` + `assignment=unassigned`). |
| `number_not_found` | 404 | `PATCH /api/admin/numbers/{id}` | Номер не найден. |
| `team_not_found` | 404 | `PATCH /api/admin/numbers/{id}`, `PATCH/POST /api/admin/users` | Переданный `team_id` не существует. |
| `twilio_error` | 502/503 | `POST /api/admin/numbers/sync` | Сбой Twilio API при sync номеров (сеть/5xx/таймаут/аутентификация) — [ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md). |
| `twilio_not_configured` | 503 | `POST /api/admin/numbers/sync` | `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` не заданы. |
