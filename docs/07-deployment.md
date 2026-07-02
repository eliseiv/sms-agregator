# 07. Deployment

Стек — см. [02-tech-stack.md](./02-tech-stack.md). Домен сервиса — **novirell.shop** (A-запись направлена на сервер). Топология развёртывания — [ADR-0007](./adr/ADR-0007-deploy-behind-shared-edge-nginx.md).

Два окружения с **разными** compose-файлами:
- **Локальная разработка** — `docker-compose.yml` (self-contained, публикует порт наружу для отладки).
- **Production** — `docker-compose.yml` + оверлей `docker-compose.prod.yml` (наружу закрыт, публичный трафик — через общий edge-nginx соседнего сервиса).

> Файлы `docker-compose.yml` / `docker-compose.prod.yml` — зона devops. Здесь описана **требуемая** топология, которой они обязаны соответствовать.

## Сервисы (общие для обоих окружений)

| Сервис | Образ / сборка | Роль | Зависимости / рестарт |
| --- | --- | --- | --- |
| `postgres` | `postgres:16-alpine` | БД. Volume `pgdata`. Healthcheck `pg_isready -U $POSTGRES_USER`. | `restart: unless-stopped`. |
| `redis` | `redis:7-alpine` | Сессии, pending-токены, rate-limit. Healthcheck `redis-cli ping`. | `restart: unless-stopped`. |
| `migrate` | сборка из `Dockerfile` | One-off `alembic upgrade head`. | `depends_on: postgres (healthy)`; `restart: "no"`. |
| `app` | сборка из `Dockerfile` | FastAPI + delivery loop. Контейнер слушает `:8000`. Healthcheck `GET /health`. | `depends_on`: postgres+redis healthy, `migrate` completed_successfully; `restart: unless-stopped`. |

`postgres`/`redis` наружу **не** публикуются ни в одном окружении (доступны только по внутренней сети compose). Volume `./data` (SQLite) — **удаляется** (данные в PostgreSQL).

Порядок запуска: `postgres` (healthy) → `redis` (healthy) → `migrate` (upgrade head, завершается) → `app`.

## Сетевая топология и окружения

### Локальная разработка (`docker-compose.yml`)

- Все сервисы — в **default**-сети, создаваемой compose автоматически (никаких внешних сетей). `global_network` **не используется** (её нет на сервере; для локали внешняя сеть не нужна).
- `app` публикует порт `8137:8000` — доступ с хоста разработчика на `http://localhost:8137`.
- `PUBLIC_BASE_URL=http://localhost:8137`, `COOKIE_SECURE=false` (HTTP локально), `VERIFY_TWILIO_SIGNATURE=false` (нет публичного webhook).
- TLS/reverse-proxy не требуется.

### Production (`docker-compose.yml` + `docker-compose.prod.yml`)

Сервер уже несёт соседний сервис mail-agregator: его **edge-nginx** (контейнер `mas-nginx`) владеет портами **80/443** и обслуживает `postapp.store`; существует внешняя docker-сеть **`mas-net`**. Нашего `global_network` на сервере **нет** — он не используется в prod.

Оверлей `docker-compose.prod.yml`:
- `app` подключается к **существующей внешней** сети `mas-net` (`external: true`, имя `mas-net`) — чтобы `mas-nginx` дотягивался до него по имени контейнера. Имя/алиас контейнера app — `sms-aggregator-app` (upstream nginx: `sms-aggregator-app:8000`).
- `app` публикует порт **только на loopback**: `127.0.0.1:8137:8000` (для локальной отладки/healthcheck на сервере); наружу (0.0.0.0) **не** открыт. Публичного порта у нашего сервиса нет — 80/443 остаются за `mas-nginx`.
- `postgres`/`redis` — во внутренней default-сети compose, к `mas-net` **не** подключаются.

Публичный трафик `https://novirell.shop`:

```mermaid
flowchart LR
    Internet[[novirell.shop / postapp.store]] -->|80/443| N[mas-nginx edge]
    N -->|vhost postapp.store| MA[mail-agregator app]
    N -->|ДОБАВОЧНЫЙ vhost novirell.shop, upstream sms-aggregator-app:8000, mas-net| APP[sms-aggregator-app :8000]
    APP --> PG[(postgres)]
    APP --> RD[(redis)]
    APP -->|Bot API| TG[[Telegram]]
    Twilio[[Twilio]] -->|POST webhook| N
```

- В `mas-nginx` добавляется **additive-only** vhost `novirell.shop` с `proxy_pass http://sms-aggregator-app:8000` (по `mas-net`) и стандартными proxy-заголовками: `X-Forwarded-Proto https`, `X-Forwarded-For`, `Host`, `X-Request-ID` (проброс). Существующий vhost `postapp.store` **не изменяется** — mail-agregator и его edge продолжают работать без деструктивных правок.
- **TLS** — хостовой `certbot` (тот же механизм, что для `postapp.store`): webroot `/var/www/certbot`, сертификаты в `/etc/letsencrypt`. Сертификат `novirell.shop` выпускается/продлевается тем же certbot; `mas-nginx` монтирует `/etc/letsencrypt` и `/var/www/certbot` (как для соседа) и терминирует TLS для обоих доменов.
- За прокси приложение работает по HTTP внутри `mas-net`, но снаружи — только HTTPS. `COOKIE_SECURE=true`, `SecurityHeaders`/HSTS активны (см. [08-security.md](./08-security.md)); приложение доверяет `X-Forwarded-Proto` от `mas-nginx` при построении `PUBLIC_BASE_URL`-схемы и валидации подписи Twilio.

**Инвариант:** правки для нашего сервиса — только **аддитивные** (новый vhost + новый сертификат + подключение app к `mas-net`). Ничто в конфигурации mail-agregator/`postapp.store` не изменяется деструктивно.

## Dockerfile

Дополнительно к текущему копированию `app/` — `COPY` каталогов `shared/`, `migrations/`, `scripts/`, файла `alembic.ini`, а также `app/api/templates/` и `app/api/static/` (если не покрыты `COPY app`). Установка зависимостей из `requirements.txt` (см. `Q-TECH-1`).

## Переменные окружения

Полный набор (`.env`). Секреты — только через env/secret manager, не в коде.

### Новые / изменённые

| Переменная | Пример | Назначение |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://sms:pass@postgres:5432/sms` | Подключение к PostgreSQL (asyncpg). Заменяет `DATABASE_PATH`. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis. |
| `APP_ENV` | `production` | Окружение. |
| `ADMIN_LOGIN` | `admin` | Логин первичного super_admin (seed). Заменяет `ADMIN_USERNAME`. |
| `ADMIN_PASSWORD` | `<strong>` | Пароль первичного super_admin (seed, argon2). |
| `SESSION_TTL_SECONDS` | `1209600` | TTL скользящей сессии. |
| `SESSION_ABSOLUTE_TTL_SECONDS` | `2592000` | Абсолютный потолок жизни сессии. |
| `SETUP_SESSION_TTL_SECONDS` | `900` | TTL setup-сессии (`/set-password`). |
| `LOGOUT_STICKY_TTL_SECONDS` | `2592000` | TTL cookie-маркера `sms_logged_out` (safety-net; первичный сброс — явный вход). [ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md). |
| `COOKIE_SECURE` | `true` (prod) / `false` (локально) | `Secure` на cookies. В prod — `true` (HTTPS через edge). |
| `LOGIN_FAILURE_THRESHOLD` | `5` | Порог неверных паролей до lockout. |
| `LOGIN_LOCKOUT_MINUTES` | `15` | Длительность lockout. |
| `TG_AUTH_INIT_DATA_TTL_SECONDS` | `300` | TTL `auth_date` initData (5 мин). |
| `TG_PENDING_LINK_TTL_SECONDS` | `900` | TTL pending-токена Mini App SSO. |
| `TG_MAX_LINKS_PER_USER` | `10` | Мягкий потолок привязок на пользователя. |
| `TELEGRAM_WEBAPP_URL` | `https://novirell.shop` | URL Mini App (кнопка бота / CSP `frame-ancestors`). |
| `TELEGRAM_WEBHOOK_SECRET` | `<random ≥32 симв.>` | **Новый** ([ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md)). Секрет-токен вебхука: сверяется с заголовком `X-Telegram-Bot-Api-Secret-Token` в `POST /api/telegram/webhook` и передаётся в `setWebhook(secret_token=...)`. Только через env/secret manager. |

### Сохраняемые

`SERVICE_NAME`, `PUBLIC_BASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PROXY_URL`, `DELIVERY_RETRY_INTERVAL_SECONDS`, `DELIVERY_MAX_ATTEMPTS`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `VERIFY_TWILIO_SIGNATURE`, `TWILIO_SIGNATURE_HEADER`, `TIMEZONE`.

> **Смена бота ([ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md)):** `TELEGRAM_BOT_TOKEN` заменяется токеном **нового** бота — он используется и для HMAC-валидации initData (`/api/telegram/auth`), и для `sendMessage`/доставки, и для webhook-операций. После смены токена старые initData инвалидируются, webhook/меню нужно настроить заново (см. §«Одноразовые операции Telegram»).

**Production-значения домена:**
- `PUBLIC_BASE_URL=https://novirell.shop`
- `TELEGRAM_WEBAPP_URL=https://novirell.shop`
- `VERIFY_TWILIO_SIGNATURE=true` (публичный HTTPS-webhook)
- Twilio webhook (Messaging → номер): `https://novirell.shop/api/webhooks/twilio/sms`
- Telegram Bot Mini App button / `setChatMenuButton`: `https://novirell.shop`

### Удаляемые

`DATABASE_PATH`, `ADMIN_USERNAME`, `ADMIN_TOKEN`, `TELEGRAM_POLLING_ENABLED`, `TELEGRAM_POLL_TIMEOUT_SECONDS`, `TWILIO_NUMBERS_SYNC_ENABLED`, `TWILIO_NUMBERS_SYNC_INTERVAL_SECONDS`.

Дополнительно для `postgres`-сервиса: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (согласованы с `DATABASE_URL`).

## Применение миграций

- Штатно: сервис `migrate` выполняет `alembic upgrade head` до старта `app`. `app` при старте миграции **не** применяет (только `init_engine("api")` в lifespan).
- Ручной откат: `docker compose run --rm migrate alembic downgrade -1`.

## Одноразовая миграция данных (SQLite → PostgreSQL)

Запускается **после** `alembic upgrade head`, до перевода трафика:

```
docker compose run --rm app python -m scripts.migrate_sqlite_to_pg \
  --sqlite /path/to/service.db \
  --database-url "$DATABASE_URL" \
  --orphan-team-name Legacy
```

Скрипт идемпотентен (`ON CONFLICT DO NOTHING`, сохранение id, `setval` sequences), печатает отчёт по строкам и списку осиротевших/мульти-проектных пользователей (см. [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md)). Проверки после прогона — [06-testing-strategy.md](./06-testing-strategy.md) §20.

## Одноразовый импорт номеров как unassigned (SQLite → PostgreSQL)

Источник — [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md). Отдельный скрипт `scripts/import_numbers.py` — импорт ~328 номеров из старой SQLite (`data/service.db`, таблица `twilio_numbers`: `phone_number`, `label`, `is_active`) в `phone_numbers` как **unassigned** (`team_id = NULL`, `added_by_user_id = NULL`). **Только номера** — projects/teams/users/deliveries НЕ переносятся (это независимо от полной миграции [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md)).

```
docker compose run --rm app python -m scripts.import_numbers \
  --sqlite /path/to/service.db --database-url "$DATABASE_URL"
```

Идемпотентен: `INSERT ... ON CONFLICT (phone_number) DO NOTHING` — повторный прогон не создаёт дублей и не перезаписывает уже назначенные номера. Печатает отчёт: прочитано / вставлено / пропущено (конфликт). После импорта super_admin распределяет номера по командам через `/admin` (секция unassigned) → `PATCH /api/admin/numbers/{id}`. Проверки — [06-testing-strategy.md](./06-testing-strategy.md) §21.

## Одноразовые операции Telegram (webhook / меню / домен)

Источник — [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md). Выполняются один раз при развёртывании нового бота (вне CD-цикла), после того как `app` доступен по `https://novirell.shop`:

1. **`setWebhook`** — зарегистрировать вебхук с секрет-токеном:
   ```
   curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -d "url=https://novirell.shop/api/telegram/webhook" \
     -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
   ```
   Telegram будет слать апдейты с заголовком `X-Telegram-Bot-Api-Secret-Token: ${TELEGRAM_WEBHOOK_SECRET}`; приложение сверяет его (§ [05-api-contracts.md](./05-api-contracts.md) §3a).
2. **`setMyCommands`** — меню бота содержит **только `/start`** (либо пустое):
   ```
   curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" \
     -H "Content-Type: application/json" \
     -d '{"commands":[{"command":"start","description":"Открыть приложение"}]}'
   ```
   Прочие команды не регистрируются (весь функционал — в Mini App).
3. **Домен Mini App в @BotFather (ручной шаг, вне кода):** у нового бота задать домен Mini App `novirell.shop` (BotFather → Bot Settings → Configure Mini App / Domain). Без этого кнопка `web_app` не откроется.
4. **Проверка:** `getWebhookInfo` показывает `url=https://novirell.shop/api/telegram/webhook`, `has_custom_certificate=false`, отсутствие ошибок доставки; `/start` в чате бота возвращает приветствие + кнопку «Открыть приложение».

> При смене бот-токена (новый бот) шаги 1–3 повторяются для нового бота; старый вебхук на прежнем боте можно снять `deleteWebhook`.

## Health и наблюдаемость

- `GET /health` → `200 {"status":"ok","service":...}` — docker healthcheck `app`.
- Логи: внешние логгеры (`twilio`, `httpx`) на уровне WARNING (не логировать URL с токенами); секреты в redact-list.

## CI/CD (GitHub Actions)

Деплой автоматизирован через GitHub Actions (зона devops; здесь — требуемый контракт pipeline).

**CI (на PR/push):**
- `ruff format --check` + `ruff check` (lint), `mypy` (type-check) — команды из [02-tech-stack.md](./02-tech-stack.md).
- `pytest` с сервисами PostgreSQL 16 + Redis 7 (GitHub Actions `services:`), `DATABASE_URL`/`REDIS_URL` указывают на них; прогон миграций + тестов ([06-testing-strategy.md](./06-testing-strategy.md)).

**CD (на push в основную ветку, после зелёного CI):**
- Доступ на сервер по SSH. **Ровно 3 секрета репозитория:** `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`. Прочие значения (`.env` prod) хранятся на сервере, не в GitHub.
- Шаги: SSH → `git pull` (или доставка кода) → **сборка образа на сервере** (`docker compose ... build`, локальный образ, без внешнего registry) → `migrate` (`alembic upgrade head`) **до** старта/обновления `app` → recreate `app` → **healthcheck** `GET http://127.0.0.1:8137/health`.
- **Откат:** при неуспешном healthcheck — вернуть предыдущий образ/коммит (`docker compose up -d` на прежнем теге) и не переключать трафик; edge-nginx продолжает указывать на прежний живой контейнер. См. `TD-006` (тег образа) в [100-known-tech-debt.md](./100-known-tech-debt.md).
- Prod-команда запуска: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.

## Применение миграций и первичная инфраструктура (one-off)

Помимо автоматического `migrate`-сервиса, при первом развёртывании на сервере выполняются разовые операции (вне CD-цикла):
1. Убедиться, что внешняя сеть `mas-net` существует (создана mail-agregator). Наш сервис её только **использует** (`external: true`), не создаёт.
2. Выпустить TLS-сертификат `novirell.shop` хостовым certbot (webroot `/var/www/certbot`) — тем же способом, что `postapp.store`.
3. Добавить additive vhost `novirell.shop` в `mas-nginx` (upstream `sms-aggregator-app:8000`), reload nginx. Конфиг `postapp.store` не трогать.

## Порядок деплоя (первый переход на PG, production)

1. One-off инфраструктура: проверить `mas-net`; выпустить сертификат `novirell.shop`; добавить vhost в `mas-nginx`.
2. Поднять `postgres`, `redis` (внутренняя сеть compose).
3. `migrate` → `alembic upgrade head`.
4. **(Опционально)** Одноразовый перенос данных SQLite → PostgreSQL — если есть legacy `service.db`:
   ```
   docker compose run --rm app python -m scripts.migrate_sqlite_to_pg \
     --sqlite /path/to/service.db --database-url "$DATABASE_URL" --orphan-team-name Legacy
   ```
   Проверить инварианты миграции (см. [06-testing-strategy.md](./06-testing-strategy.md) §20). Шаг пропускается для чистой установки.
5. Поднять `app` (`docker-compose.prod.yml`, `mas-net`, `127.0.0.1:8137`), проверить `GET http://127.0.0.1:8137/health` и `https://novirell.shop/health` через edge.
6. **(Опционально)** Импорт номеров как unassigned — если есть legacy `service.db` с `twilio_numbers` (см. §«Одноразовый импорт номеров»): `docker compose run --rm app python -m scripts.import_numbers --sqlite /path/to/service.db --database-url "$DATABASE_URL"`. Затем распределить пул через `/admin`.
7. Настроить Twilio webhook на `https://novirell.shop/api/webhooks/twilio/sms`.
8. **Telegram (новый бот, [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md)):** задать домен Mini App `novirell.shop` в @BotFather; `setWebhook(url=https://novirell.shop/api/telegram/webhook, secret_token=$TELEGRAM_WEBHOOK_SECRET)`; `setMyCommands` только `/start`; проверить `/start` в чате бота (см. §«Одноразовые операции Telegram»).

> Дублирующая справка по one-off командам переноса/импорта данных — см. шаги 4, 6 выше, [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md) и [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md).
