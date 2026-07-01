# Deployment runbook — sms-agreagtor (novirell.shop)

Прод-сервер Hetzner `49.12.189.77` (Ubuntu 26.04). На нём **уже** работает
`mail-agregator` (домен `postapp.store`). Наш сервис разворачивается
**additive-only** — не затрагивая mail-agregator.

## Топология (по итогам инспекции сервера)

- Хостовые `80/443` держит контейнер `mas-nginx` (nginx:1.27-alpine) из
  mail-agregator. Собственного edge sms **не поднимает** — конфликта за порты нет.
- TLS у mail-agregator: **хостовый** `certbot` (webroot `/var/www/certbot`,
  `certbot.timer` дважды в сутки), `/etc/letsencrypt` bind-mount **RO** в
  `mas-nginx`. Мы выпускаем cert `novirell.shop` тем же механизмом.
- Edge `mas-nginx` рендерит `conf.d/*.conf` из
  `/opt/mail-agregator/deploy/nginx/templates/*.template` (envsubst при старте).
- Сеть edge — bridge `mas-net` (external, `172.18.0.0/16`). `global_network`
  из docs/07 на сервере **не существует**.
- Порт `8137` на хосте свободен. DNS `novirell.shop` **уже** указывает на
  `49.12.189.77` (A-запись подтверждена).

### Как novirell.shop обслуживается без конфликта с postapp.store

1. В `mas-nginx` добавляется **отдельный vhost** `novirell.shop`
   (`deploy/nginx/novirell.shop.conf`) рядом с `postapp.store`. Файл vhost
   mail-agregator не изменяется.
2. vhost проксирует на `http://sms-aggregator-app:8000` по docker-DNS во
   внешней сети `mas-net`, к которой app подключён (`docker-compose.prod.yml`).
   App наружу не публикуется (только `127.0.0.1:8137` для локального health).
3. Cert `novirell.shop` выпускается хостовым `certbot --webroot` и читается
   `mas-nginx` из RO-mount `/etc/letsencrypt`.

**Механизм инъекции vhost (двухуровневый, по итогам инспекции mas-nginx):**

- **Primary — статический conf.d-файл (envsubst-free).** `deploy/nginx/novirell.shop.conf`
  копируется `docker cp` в `mas-nginx:/etc/nginx/conf.d/novirell.shop.conf`.
  Их `nginx.conf` содержит `include /etc/nginx/conf.d/*.conf;` → файл грузится.
  Их envsubst к нему НЕ применяется (это не template). Применяется немедленно
  (`nginx -t` → `nginx -s reload`).
- **Durability across recreate — template в их templates dir.** conf.d НЕ
  bind-mount (эфемерна, регенерится из templates при старте контейнера),
  поэтому копия кладётся и как
  `/opt/mail-agregator/deploy/nginx/templates/novirell.shop.conf.template` —
  alpine-entrypoint регенерирует её в conf.d при пересоздании `mas-nginx`
  (передеплой mail-agregator). Vhost переживает recreate.

**Envsubst-safe (подтверждено инспекцией).** Entrypoint
`20-envsubst-on-templates.sh` (сток nginx:alpine) вызывает `envsubst` с ЯВНЫМ
allowlist `defined_envs` = `${VAR}` по всем env-переменным контейнера
(фактически на сервере: `SERVER_DOMAIN`, `NGINX_VERSION`, `NJS_RELEASE`,
`NJS_VERSION`, `DYNPKG_RELEASE`, `PKG_RELEASE`, `PATH`; `NGINX_ENVSUBST_FILTER`
не задан). Это **не** bare-envsubst: `envsubst "$defined_envs"` заменяет только
перечисленные имена, остальные `$var` сохраняет. Наши переменные
(`$host`, `$uri`, `$request_uri`, `$remote_addr`, `$proxy_add_x_forwarded_for`,
`$sms_upstream`) в allowlist не входят → не подставляются (тот же приём, что в
их `default.conf.template` с bare `$host`/`$uri`). Экранирование `${DOLLAR}`
здесь неприменимо (env-переменной `DOLLAR` в контейнере нет — сломало бы конфиг).

**Idempotent + self-heal.** Повторная установка перезаписывает те же файлы,
`nginx -t` перед `reload`; при провале `nginx -t` vhost novirell.shop удаляется
и `mas-nginx` reload — postapp.store не страдает. Инвазивность к дереву
mail-agregator минимальна: добавляется один **новый** untracked-файл в их
`templates/` (их `git pull` его сохраняет; аналогично их же untracked
`.htpasswd_trevionly`). Если их деплой выполнит `git clean -fdx` (снесёт
untracked) — **следующий запуск sms-деплоя** (или ручной `bash deploy/deploy.sh`)
восстановит и conf.d, и template.

## GitHub Secrets — РОВНО 3

| Secret | Значение |
| --- | --- |
| `SSH_HOST` | `49.12.189.77` |
| `SSH_USER` | `root` |
| `SSH_PRIVATE_KEY` | приватный ключ деплой-пары (см. ниже) |

Путь деплоя (`/opt/sms-agreagtor`) и домен (`novirell.shop`) — **захардкожены**
в workflow/скриптах, не в секретах. Registry не используется — образ собирается
на сервере.

**Доставка кода без git на сервере (самодостаточность 3 секретов).** Workflow
`Deploy` делает `actions/checkout` на раннере и передаёт рабочее дерево
**архивом поверх SSH** тем же ключом:
`tar czf - --exclude=.git --exclude=.env … . | ssh 'tar xzf - -C /opt/sms-agreagtor'`
(перед распаковкой всё, кроме `.env`/`secrets`, удаляется — эквивалент
`rsync --delete` без зависимости от rsync на сервере). Серверу **не нужен**
git-доступ к GitHub — четвёртой учётки (git-credential) нет. `.env`/`secrets`
на сервере сохраняются (в архив не входят).

### Генерация деплой-ключа (шаг реального деплоя, НЕ коммитить приватный ключ)

```bash
# локально:
ssh-keygen -t ed25519 -f sms_deploy_key -C "sms-agreagtor-cd" -N ""
# публичную часть — на сервер:
ssh-copy-id -i sms_deploy_key.pub root@49.12.189.77   # или вручную в ~/.ssh/authorized_keys
# приватную часть sms_deploy_key -> GitHub Secret SSH_PRIVATE_KEY (Settings → Secrets)
# затем удалить локальные копии ключа
```

## Первичный запуск (однократно, вручную на сервере под root)

git на сервере **не используется**. Код доставляется архивом (как в CD).

```bash
# 1. Доставить дерево репозитория на сервер (с машины оператора, git-free):
tar czf - --exclude=.git --exclude=.env --exclude=secrets . \
  | ssh root@49.12.189.77 'mkdir -p /opt/sms-agreagtor && tar xzf - -C /opt/sms-agreagtor'
#    (одноразовый ручной `git clone` тоже допустим — но ЯВНО operator-only,
#     вне CD и вне периметра 3 GitHub Secrets.)

# 2. Запустить bootstrap — создаст .env из .env.prod.example и остановится:
bash /opt/sms-agreagtor/deploy/bootstrap.sh
# 3. Заполнить секреты:
nano /opt/sms-agreagtor/.env
# 4. Повторный запуск: выпустит cert novirell.shop, поставит vhost, поднимет стек:
bash /opt/sms-agreagtor/deploy/bootstrap.sh
```

## Обычный деплой (автоматически)

`push` в `main` → workflow **Deploy**: `actions/checkout` → доставка дерева
**архивом поверх SSH** (git на сервере не нужен) → `deploy/deploy.sh`:

1. Снимок текущего образа app (`:rollback`).
2. `docker compose build` (на сервере).
3. Миграции: `up -d postgres redis` → `run --rm migrate` (**до** старта нового кода).
4. `docker compose up -d`.
5. Poll `http://127.0.0.1:8137/health` (30×3s). Провал → откат образа app на
   `:rollback` + `up -d app`, exit 1.
6. Переустановка edge-vhost novirell.shop: `docker cp` в conf.d (primary) +
   template (durability) + `nginx -t` + reload.
7. Best-effort внешняя проверка `https://novirell.shop/health`.

## Откат

- **Автоматический** (в deploy.sh): при провале healthcheck образ app
  возвращается на `:rollback` (тег `sms-aggregator-app:rollback`).
- **Ручной откат кода:** задеплоить предыдущий коммit (push revert в main или
  повторный запуск CD с прошлым деревом).
- **Схема БД:** миграции forward-only. Откат схемы — вручную:
  `docker compose -f docker-compose.prod.yml run --rm migrate alembic downgrade -1`.
- **Edge-vhost:** при провале `nginx -t` vhost novirell.shop удаляется и
  `mas-nginx` перезагружается — postapp.store не страдает.

### ⚠️ Rollback образа ↔ forward-only миграции (обязательное требование)

Авто-откат возвращает **образ** app на предыдущий, но **уже применённые
миграции НЕ откатывает** (`migrate` выполняет `alembic upgrade head` до старта
кода, схема forward-only). Чтобы image-rollback был безопасен, каждая миграция
**обязана быть backward-compatible** (паттерн **expand/contract**): новая схема
работоспособна для предыдущей версии кода (аддитивные изменения; удаление
столбцов/таблиц/ограничений — отдельным поздним «contract»-релизом, когда старый
код уже выведен). Иначе после отката образа старый код может встретить схему,
которую не понимает. Фиксацию этого правила в контракте делает **architect**
(docs/07); здесь — эксплуатационная заметка.

## Пиннинг базовых образов (опционально, не блокер)

`postgres:16-alpine` и `redis:7-alpine` запиннены по минорной версии. Для полной
воспроизводимости можно дополнительно запиннить по digest, напр.:
`image: postgres:16-alpine@sha256:<digest>` (digest снять с сервера:
`docker images --digests | grep postgres`). Требует ручного обновления digest
при апгрейде — поэтому оставлено на усмотрение (по образцу mail-agregator,
который пиннит по digest только `minio/mc`).

## Продление TLS

Хостовый `certbot.timer` уже продлевает **все** сертификаты (включая
novirell.shop после первого выпуска) через webroot. Отдельная настройка не нужна.

## Что НЕ трогаем у mail-agregator

- `docker-compose.yml`, `nginx.conf`, `default.conf.template`, `.env`,
  контейнеры `mas-*`, сеть `mas-net`, сертификат `postapp.store`.
- Единственное дополнение в дереве mail-agregator — **новый** файл
  `templates/novirell.shop.conf.template` (их файлы не изменяются).
