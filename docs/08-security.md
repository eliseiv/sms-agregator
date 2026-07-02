# 08. Security

Auth, секреты и угрозы описаны явно с первого дня. Endpoints и коды — [05-api-contracts.md](./05-api-contracts.md).

## 1. Пароли — argon2id

- Хеширование `argon2-cffi` (argon2id), singleton в `app/core/security.py`: `hash()`, `verify()`, `needs_rehash()`.
- Параметры — дефолты `argon2-cffi` (memory-hard); при `needs_rehash()` после успешного verify — прозрачное перехеширование.
- `users.password_hash` — `NULL` для созданных админом (до `/set-password`) и после reset.
- **Единственный super_admin.** `seed_admin()` при старте обеспечивает ровно одного `super_admin`: ищет существующего (partial-UNIQUE `users_single_super_admin`, см. [04-data-model.md](./04-data-model.md)); если найден и его `username != ADMIN_LOGIN` — **переименовывает** его (UPDATE `username`+`password_hash`), иначе обновляет пароль; если не найден — INSERT. Смена `ADMIN_LOGIN` в env не создаёт второго админа. Partial-UNIQUE индекс в БД — страховка от гонок/ручных вставок.
- **Пароль super_admin меняется только через env + рестарт.** `POST /api/admin/users/{id}/reset` **запрещён** для роли `super_admin` (`403 cannot_reset_super_admin`, включая self-reset). Иначе учётка админа временно попадала бы в `password_reset_required=true` и была бы claimable через `/set-password` любым, кто знает логин, до следующего рестарта (где `seed_admin` восстановит hash). Смена пароля админа — через `ADMIN_PASSWORD` + перезапуск сервиса. Это защита от временного захвата (аналог запрета `DELETE` для super_admin).
- **Анти-timing:** при несуществующем логине или `password_hash IS NULL` login всё равно выполняет verify против фиксированного dummy-hash — одинаковое время ответа.
- **Политика пароля** (`/set-password`): минимум 8 символов; `password == password_confirm`. (Дополнительные правила — при необходимости `Q`.)

## 2. Сессии — Redis

- `app/infrastructure/sessions.py`: `SessionStore` (основные сессии) + `SetupSessionStore` (setup-сессии `/set-password`).
- Сессия — opaque-токен в cookie `sms_session` (HttpOnly, `Secure` при `COOKIE_SECURE`, `SameSite=Lax`), значение в Redis `session:{token}` с TTL `SESSION_TTL_SECONDS` (скользящий) и абсолютным потолком `SESSION_ABSOLUTE_TTL_SECONDS`.
- `user_sessions:{user_id}` (Redis SET) — для массового revoke при reset/delete.
- Промежуточные: `sms_login` (шаг-1 логина, короткий TTL), `sms_setup` (TTL `SETUP_SESSION_TTL_SECONDS`).
- Logout: `DEL session:{token}` + clear cookies; `telegram_links` **не трогаются** ([ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)).

## 3. CSRF — double-submit

- Cookie `sms_csrf` (не HttpOnly — читается `csrf.js` / рендерится как hidden-поле в SSR-формах) + заголовок/поле `csrf_token`. CSRF-middleware сверяет для `POST/DELETE/PATCH` **только когда парный CSRF-cookie уже установлен**. `sms_csrf` выпускается вместе с аутентифицированной сессией (`sms_session`) и вместе с setup-сессией (`sms_setup`) на `/set-password`.
- **Exempt (double-submit невозможен на этом шаге):**
  - `POST /api/webhooks/twilio/sms` — защита: подпись Twilio;
  - `POST /api/telegram/auth` — защита: HMAC initData;
  - `POST /login` и `POST /login/password` — на этих шагах ещё нет ни `sms_session`, ни `sms_csrf` (есть только промежуточная `sms_login` без CSRF-токена), поэтому double-submit физически невозможен. Защита — rate-limit (`LIMIT_LOGIN` per IP + `LIMIT_LOGIN_USERNAME` per username, см. §4) + anti-timing/анти-энумерация (§6). Это соответствует референсу mail-agregator.
- **Требуют CSRF:** `POST /set-password` (есть `sms_setup`+CSRF), `POST /logout` и все `/api/admin/*`, `/api/numbers*` (есть `sms_session`+`sms_csrf`). Мутации со страницы участника `/app` (добавление/удаление номера) идут через `/api/numbers*` и подчиняются этому же правилу.
- **SSR-страницы за сессией** (`/app`, `/admin`, `/admin/teams`) требуют валидной `sms_session` (`require_authenticated`/`require_admin`). Корневой `GET /` — публичный **диспетчер** (только `302`-редирект по роли, контента не отдаёт), поэтому CSRF/guard не применяются ([ADR-0008](./adr/ADR-0008-root-route-and-per-role-landing.md), [05-api-contracts.md](./05-api-contracts.md) §7).
- `MethodOverride`-middleware поддерживает `_method` из HTML-форм (SSR без JS).

## 4. Rate-limiting и lockout

Счётчики в Redis (`app/infrastructure/rate_limit.py`).

| Область | Лимит (рекоменд.) | Ключ |
| --- | --- | --- |
| `POST /login` | напр. 10/min | per IP + per username |
| `POST /login/password` | напр. 10/min | per IP + per username |
| `POST /set-password` | напр. 10/min | per IP + per setup-session |
| `POST /api/telegram/auth` | 30/min IP (до HMAC) + 10/min per `telegram_user_id` (после HMAC) | IP / tg_user_id |

- **Lockout:** после `LOGIN_FAILURE_THRESHOLD` неверных паролей — `users.lockout_until = now() + LOGIN_LOCKOUT_MINUTES`; login отклоняется до истечения; audit `lockout_triggered`. Успешный вход/истечение сбрасывает `failed_login_attempts`.
- Rate-limit `429` — независимо от lockout (защита от brute-force по многим аккаунтам).

## 5. HMAC-валидация Telegram initData

`app/telegram/init_data.py::verify_init_data()` — копия эталона (см. [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)):

```
1. parse init_data (form-urlencoded); извлечь hash.
2. data_check_string = "\n".join(sorted("k=v" for k,v in pairs без hash)).
3. secret_key = HMAC_SHA256(key="WebAppData", msg=TELEGRAM_BOT_TOKEN).
4. computed = hex(HMAC_SHA256(key=secret_key, msg=data_check_string)).
5. constant-time compare → fail: 401 invalid_init_data.
6. TTL: now - auth_date > TG_AUTH_INIT_DATA_TTL_SECONDS (300) → 401 init_data_expired.
7. telegram_user_id = json(pairs["user"])["id"].
```

- TTL 5 минут жёстче рекомендованных Telegram 24ч — украденный initData нельзя переиспользовать позже.
- `telegram_user_id` берётся только из подписанного payload, не из тела запроса.

## 6. Анти-энумерация логинов

- `POST /login` (шаг-1) отвечает идентично для существующего и несуществующего логина (`303 → /login/password`, всегда ставит `sms_login`).
- Ошибка появляется только на шаге-2 как `invalid_credentials`, без различения «нет пользователя» / «неверный пароль».
- `POST /api/admin/users` возвращает `409 username_taken` только админу (не публично).

## 6a. Принятый остаточный риск: первичная активация аккаунта

Флоу первичной установки пароля по ТЗ: админ создаёт пользователя, указывая **только логин** (`password_hash=NULL`, `password_reset_required=true`); пользователь входит под логином и сам задаёт пароль на `/set-password`. Out-of-band секрет (invite-токен / обязательный Telegram-proof) **не** используется.

**Остаточный риск (принят владельцем, [100-known-tech-debt.md](./100-known-tech-debt.md) TD-010):** актор, знающий существующий логин в состоянии `password_reset_required=true`, может занять неактивированный аккаунт в окне между созданием (или admin reset) и первым легитимным входом.

**Компенсирующие меры:** секретность логина; rate-limit на `/login` и `/set-password` (§4); короткое окно активации; запрет reset для super_admin (§1). Возможное будущее ужесточение — Telegram Mini App proof или invite-токен на `/set-password` (TD-010).

## 7. Security-заголовки / CSP

`SecurityHeaders`-middleware:
- `Content-Security-Policy`: `default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors https://telegram.org` (Mini App открывается во фрейме Telegram; уточнить `frame-ancestors` под `TELEGRAM_WEBAPP_URL`).
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options` — согласовать с `frame-ancestors` (для Mini App не ставить `DENY`).
- `Strict-Transport-Security` при `COOKIE_SECURE`/HTTPS.

**За общим edge-nginx (production, [07-deployment.md](./07-deployment.md), [ADR-0007](./adr/ADR-0007-deploy-behind-shared-edge-nginx.md)):** TLS терминируется на `mas-nginx` (домен `novirell.shop`); приложение работает по HTTP внутри сети `mas-net`, поэтому доверяет `X-Forwarded-Proto: https` от edge. `COOKIE_SECURE=true` в prod (cookies помечаются `Secure`, отдаются только по HTTPS). Подпись Twilio (§8) валидируется по публичному URL `PUBLIC_BASE_URL + path` (`https://novirell.shop/api/webhooks/twilio/sms`), а не по внутреннему адресу контейнера — edge должен пробрасывать оригинальный `Host` и `X-Forwarded-Proto`. `SecurityHeaders`/HSTS выставляются приложением и проходят через edge без перезаписи.

## 8. Подпись Twilio

- `app/infrastructure/twilio_security.py` — `RequestValidator` (Twilio SDK). При `VERIFY_TWILIO_SIGNATURE=true` webhook валидирует `X-Twilio-Signature` по `PUBLIC_BASE_URL + path + query` и form-payload.
- Если `VERIFY_TWILIO_SIGNATURE=true`, но `TWILIO_AUTH_TOKEN` пуст → `500` (fail-closed).
- `VERIFY_TWILIO_SIGNATURE=false` — только для тестов/локальной разработки.

## 9. Секреты и логирование

- Все секреты (`ADMIN_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TWILIO_AUTH_TOKEN`, `DATABASE_URL`, `REDIS_URL`) — из env, не в коде/репозитории.
- Внешние логгеры (`twilio`, `httpx`) — WARNING, чтобы не логировать URL с токенами. Ошибки доставки/аудита не пишут тела токенов.
- `deliveries.last_error` усекается и очищается от чувствительных данных.

## 10. Угрозы и митигации (сводка)

| Угроза | Митигация |
| --- | --- |
| Подмена SMS-webhook | Подпись Twilio (fail-closed). |
| Подмена/replay initData | HMAC bot-token + TTL 5 мин + constant-time compare. |
| Brute-force пароля | Rate-limit + lockout + argon2id. |
| Энумерация логинов | Единый ответ шага-1, anti-timing dummy-hash. |
| Кража cookie `sms_tg_pending` | HttpOnly, Secure, короткий TTL, одноразовый. |
| Компрометация аккаунта | admin reset → revoke всех сессий + всех `telegram_links`. |
| Захват неактивированного аккаунта | Rate-limit `/login`+`/set-password`, короткое окно, секретность логина. Принят как остаточный риск — §6a, TD-010. |
| Временный захват учётки super_admin через self-reset | Запрет `reset` для super_admin (`cannot_reset_super_admin`, §1); пароль — только через `ADMIN_PASSWORD`+рестарт. |
| Доступ к чужой команде | `VisibilityScope` + guards; участник ограничен `team_id`. |
| Утечка секретов в логи | redact внешних логгеров, WARNING-уровень. |
