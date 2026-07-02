# ADR-0004 — Telegram Mini App SSO (initData HMAC, pending-токены, self-heal, dead-links)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-01 |
| Связано | ADR-0002, ADR-0005, ADR-0011 (**амендит** §5 logout/SSO); [05-api-contracts.md](../05-api-contracts.md) §3, [08-security.md](../08-security.md) §5 |

## Context

Вход в админ-панель выполняется через **Telegram Mini App**. Требования:

- при открытии Mini App пользователь не должен каждый раз вводить логин/пароль (persistent SSO);
- получателями SMS должны быть только аккаунты с активной привязкой Telegram, установленной через Mini App ([ADR-0005](./ADR-0005-sms-addressing-via-team.md));
- старый flow подписки через бот (`/start`, long polling) удаляется;
- один бот обслуживает и логин, и push-доставку.

Референс mail-agregator уже реализует этот паттерн (ADR-0022/0024): отдельная таблица привязок, endpoint `/api/telegram/auth`, HMAC-валидация initData, pending-токены, self-heal, dead-links, расцепление logout и привязки. Переносим его логику, адаптируя имена под наш сервис.

## Decision

### 1. Таблица `telegram_links`

`telegram_user_id BIGINT PK` (= chat_id из подписанного initData) → `user_id BIGINT FK ON DELETE CASCADE` (**без UNIQUE** — 1:N, мягкий потолок `TG_MAX_LINKS_PER_USER`), `created_at`, `dead_at NULL`. Активна пока `dead_at IS NULL`. PK по `telegram_user_id` даёт атомарный upsert `ON CONFLICT (telegram_user_id) DO UPDATE`. (DDL — [04-data-model.md](../04-data-model.md).)

### 2. Endpoint `POST /api/telegram/auth`

Единая точка «доказательство владения Telegram-аккаунтом → привязка к текущему контексту». Публичный, **CSRF-exempt** (защита — HMAC initData). Тело `{"init_data": "..."}`.

Валидация `verify_init_data` (`app/telegram/init_data.py`, копия эталона): HMAC-SHA256 с `secret_key=HMAC("WebAppData", TELEGRAM_BOT_TOKEN)`, constant-time compare, TTL `auth_date` = `TG_AUTH_INIT_DATA_TTL_SECONDS` (300 c). Ошибки → `401 invalid_init_data` / `init_data_expired`. Rate-limit: 30/min per IP (до HMAC) + 10/min per `telegram_user_id` (после).

Ветвление (по наличию `sms_session`, определяет backend):
- **есть сессия** → `self_heal_link(telegram_user_id → current_user_id)` → `200 {linked:false, healed:true}` (без redirect/cookie);
- **нет сессии, привязка живая** → создать сессию → `200 {linked:true, redirect:"/"}` + Set-Cookie `sms_session`/`sms_csrf`;
- **нет сессии, привязки нет** → `create_pending(token → telegram_user_id, TTL TG_PENDING_LINK_TTL_SECONDS)` в Redis → `200 {linked:false}` + Set-Cookie `sms_tg_pending` (HttpOnly, короткий, одноразовый).

### 3. Создание привязки из pending

После успешного `POST /login/password` **или** `POST /set-password` backend читает `sms_tg_pending` → `consume_pending` (Redis one-shot) → `link_pending`: upsert `telegram_links(telegram_user_id → user_id)`; очищает cookie/токен. Audit `telegram_link_created`.

### 4. Self-heal и правило `created_at`

`self_heal_link` (best-effort, не бросает наружу) выполняется при каждом открытии Mini App залогиненным пользователем. Правило единообразно для всех точек upsert:

| Состояние строки (по PK) | Действие | `created_at` | audit |
| --- | --- | --- | --- |
| нет | INSERT | `=now()` | `telegram_link_created` |
| `user_id=current`, `dead_at IS NULL` | **NO-OP** | не меняется | нет |
| `user_id=current`, `dead_at IS NOT NULL` | UPDATE `dead_at=NULL` | `=now()` | `telegram_link_created` (replaced) |
| `user_id≠current` | UPDATE (rebound) | `=now()` | `telegram_link_rebound` |

NO-OP для живой привязки того же user критичен: он не сдвигает окно доставки и не спамит audit.

### 5. Dead-links и logout

- Bot API 403 / «bot was blocked» / «chat not found» при доставке → `telegram_links.dead_at = now()` (per-chat) + `deliveries.status='dead'`. Реактивация — при следующем успешном `POST /api/telegram/auth` того же tg-user (upsert обнуляет `dead_at`).
- **Logout расцеплён с привязкой:** `POST /logout` завершает только веб-сессию; `telegram_links` **не** удаляются (push переживает logout). Отвязка — только явным действием/reset.
  - **Амендмент (2026-07-02, [ADR-0011](./ADR-0011-sticky-logout-vs-miniapp-sso.md)):** поскольку привязка переживает logout, при следующей загрузке страницы `tg.js` авто-POSTил бы initData в `/api/telegram/auth` и мгновенно перелогинивал пользователя. Чтобы осознанный выход «залипал», `POST /logout` дополнительно ставит cookie-маркер `sms_logged_out`: `tg.js` при его наличии не делает авто-SSO, а `/api/telegram/auth` при маркере и отсутствии сессии не создаёт сессию/pending (`200 {linked:false, logged_out:true}`). `telegram_links` при этом **по-прежнему не трогаются** — push сохраняется; маркер лишь подавляет авто-создание веб-сессии до явного входа. Детали — ADR-0011.
- **admin reset password** → revoke всех сессий + **всех** `telegram_links` пользователя (`reason="password_reset"`).

## Rationale

- Отдельная таблица `telegram_links` изолирует домен привязки (жизненный цикл, `dead_at`) от `users` и даёт атомарный upsert.
- CSRF-exempt оправдан: на момент первого вызова сессии нет; аутентичность запроса гарантирует HMAC bot-token, TTL защищает от replay.
- Self-heal устраняет баг «залогинен, но привязка отсутствует → push не идёт»; NO-OP-правило предотвращает потерю сообщений из окна между заходами.
- Расцепление logout и привязки устраняет цикл «create → (фантомный) logout → create», наблюдавшийся в референсе.
- Перенос проверенной логики mail-agregator минимизирует риск.

## Consequences

**Плюсы:** удобный persistent-вход, надёжная адресация push, устойчивость к блокировкам бота, переносимость.

**Минусы / издержки:**
- Появляется публичный CSRF-exempt endpoint — требует строгого rate-limit и HMAC (см. [08-security.md](../08-security.md)).
- Push переживает web-logout — осознанный компромисс безопасности (прекращение push — блокировкой бота или admin reset).
- Нужны Redis-структуры для pending-токенов и сессий; корректная очистка одноразового `sms_tg_pending`.
- `1:N` привязки требуют мягкого потолка `TG_MAX_LINKS_PER_USER`.

## Alternatives

1. **Колонка `users.telegram_user_id UNIQUE` вместо таблицы.** Отклонено: нет места под `dead_at`/audit, неудобная перепривязка, засоряет `users`.
2. **Сохранить `/start` + long polling.** Отклонено: не даёт SSO, не проверяет владение аккаунтом криптографически, сложнее эксплуатировать; удаляется ([ADR-0005](./ADR-0005-sms-addressing-via-team.md)).
3. **Отдельный endpoint `ensure-link` для залогиненных.** Отклонено: дублирует HMAC-валидацию и CSRF-исключение; фронту пришлось бы знать про HttpOnly-сессию. Ветвление на backend по `sms_session` проще.
4. **Logout рвёт привязку.** Отклонено: в референсе привело к «само-разлогиниванию» push при фантомном logout; расцепление надёжнее.
