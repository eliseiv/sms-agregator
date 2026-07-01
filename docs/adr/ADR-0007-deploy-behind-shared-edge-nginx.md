# ADR-0007 — Деплой за общим edge-nginx соседнего сервиса (additive vhost, host certbot)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-02 |
| Связано | ADR-0001, ADR-0006; [07-deployment.md](../07-deployment.md), [08-security.md](../08-security.md) |

## Context

devops инспектировал целевой сервер. Факты:

- Порты **80/443** уже заняты edge-nginx соседнего сервиса **mail-agregator** (контейнер `mas-nginx`), обслуживающего домен `postapp.store`.
- Существует внешняя docker-сеть **`mas-net`**; сети `global_network` (которую предполагал первоначальный `docker-compose.yml`) на сервере **нет**.
- TLS терминируется **хостовым `certbot`** (webroot `/var/www/certbot`, сертификаты в `/etc/letsencrypt`).
- Домен нашего сервиса — **novirell.shop** (A-запись уже направлена на сервер).

Наш сервис (SMS-агрегатор) должен стать публично доступным по `https://novirell.shop`, **не** ломая соседа и не занимая 80/443.

## Decision

1. **Разделить окружения** двумя compose-файлами:
   - **Локальная разработка** — `docker-compose.yml`: default-сеть compose, `app` публикует `8137:8000`, HTTP, без TLS/прокси. `global_network` **не используется**.
   - **Production** — `docker-compose.yml` + оверлей `docker-compose.prod.yml`.
2. **Production-топология:**
   - `app` подключается к **существующей внешней** сети `mas-net` (`external: true`); алиас контейнера — `sms-aggregator-app`.
   - `app` публикует порт **только на loopback** — `127.0.0.1:8137:8000`; наружу (0.0.0.0) закрыт. Публичного порта у сервиса нет.
   - `postgres`/`redis` — во внутренней default-сети compose, к `mas-net` не подключаются, наружу не публикуются.
3. **Публичный трафик** `novirell.shop` идёт через **добавочный (additive-only)** vhost в существующем `mas-nginx`: `proxy_pass http://sms-aggregator-app:8000` по `mas-net`, с пробросом `Host`, `X-Forwarded-Proto https`, `X-Forwarded-For`, `X-Request-ID`. Конфиг `postapp.store` **не изменяется**.
4. **TLS** — тем же хостовым `certbot`, что и у соседа: сертификат `novirell.shop` выпускается/продлевается через webroot `/var/www/certbot`; `mas-nginx` монтирует `/etc/letsencrypt` и терминирует TLS для обоих доменов. 80/443 остаются за `mas-nginx`.
5. **CI/CD** — GitHub Actions: CI (`ruff`+`mypy`+`pytest` c PG/Redis), CD по SSH (**ровно 3 секрета** `SSH_HOST`/`SSH_USER`/`SSH_PRIVATE_KEY`), сборка образа на сервере, `migrate` до `app`, healthcheck + откат. Одноразовый перенос данных SQLite→PG ([ADR-0006](./ADR-0006-data-migration-sqlite-to-pg.md)) — опциональный шаг. Детали — [07-deployment.md](../07-deployment.md).
6. **Инвариант additive-only:** все изменения на сервере ради нашего сервиса — только добавления (новый vhost + новый сертификат + подключение `app` к `mas-net`). Ничто в mail-agregator/`postapp.store` не меняется деструктивно; сосед продолжает работать.

## Rationale

- Порты 80/443 заняты — свой edge поднять нельзя без конфликта; переиспользование `mas-nginx` через additive vhost — наименее инвазивно.
- `mas-net` уже существует — подключение к ней даёт nginx доступ к нашему `app` по имени контейнера без публикации портов наружу (минимальная поверхность атаки).
- Хостовой certbot уже настроен для соседа — тот же механизм для `novirell.shop` не плодит новых компонентов.
- Разделение compose-файлов исключает противоречие «local vs prod» (нет несуществующей `global_network` в prod; локально не нужна внешняя сеть).
- Loopback-публикация `127.0.0.1:8137` даёт healthcheck/отладку на сервере, не открывая сервис в интернет напрямую.

## Consequences

**Плюсы:** ноль конфликтов портов; сосед не затронут; минимальная сетевая поверхность; единый TLS-механизм; чёткое разделение local/prod.

**Минусы / издержки:**
- Появляется runtime-зависимость от чужого компонента (`mas-nginx`, `mas-net`): если сосед пересоздаёт edge/сеть — наш vhost/подключение нужно восстановить. Зафиксировать в runbook devops.
- Приложение обязано доверять `X-Forwarded-Proto` от edge и строить публичный URL из `PUBLIC_BASE_URL` (важно для подписи Twilio и `Secure`-cookies) — см. [08-security.md](../08-security.md) §7.
- `docker-compose.prod.yml` ссылается на внешнюю `mas-net` (`external: true`) — при её отсутствии деплой упадёт (нужна one-off проверка, [07-deployment.md](../07-deployment.md)).
- Тег образа `:latest` без registry (`TD-006`) — откат опирается на пересборку/предыдущий коммит.

## Alternatives

1. **Свой отдельный edge-nginx на 80/443.** Отклонено: порты заняты `mas-nginx`; конфликт, потребовал бы перестройки соседа.
2. **Открыть `app` напрямую на публичный порт (без прокси).** Отклонено: нет TLS-терминации, ручное управление сертификатами, ломает единый вход 443; небезопасно.
3. **Использовать `global_network`.** Отклонено: сети нет на сервере — прямое противоречие факту; в prod используем `mas-net`.
4. **Traefik/Caddy как новый reverse-proxy.** Отклонено: дублирует уже работающий `mas-nginx` и хостовой certbot; лишний компонент без выгоды для одного домена.
5. **Внешний registry для образов + pull на сервере.** Отклонено на этой итерации: сборка на сервере проще, без учётных данных registry; трассируемость отката — как `TD-006` (опционально позже).
