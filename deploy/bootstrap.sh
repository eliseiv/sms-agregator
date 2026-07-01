#!/usr/bin/env bash
# =============================================================================
# deploy/bootstrap.sh — ОДНОКРАТНАЯ первичная подготовка сервера под sms-agreagtor.
# Запускается вручную оператором на сервере ПОД ROOT, до первого CD-деплоя.
# Idempotent: повторный запуск безопасен.
#
# Что делает (всё additive, mail-agregator/postapp.store не трогаем):
#   1. Проверяет docker + compose + существование внешней сети mas-net.
#   2. Проверяет, что код sms уже доставлен в /opt/sms-agreagtor, и .env заполнен.
#   3. Выпускает TLS-cert novirell.shop хостовым certbot (webroot), решая
#      chicken-and-egg через временный HTTP-only vhost в mas-nginx.
#   4. Устанавливает полный edge-vhost novirell.shop (HTTP+HTTPS) в mas-nginx
#      (в conf.d текущего контейнера + в templates для durability) и reload.
#
# ДОСТАВКА КОДА (git на сервере НЕ используется — вне периметра 3 GitHub Secrets):
#   Код в /opt/sms-agreagtor кладётся ТЕМ ЖЕ архивным механизмом, что и CD.
#   Одноразовая первичная доставка с машины оператора (пример):
#     tar czf - --exclude=.git --exclude=.env --exclude=secrets . \
#       | ssh root@49.12.189.77 'mkdir -p /opt/sms-agreagtor && tar xzf - -C /opt/sms-agreagtor'
#   (или один раз вручную `git clone` — ЯВНО operator-only, вне CD и вне 3 секретов).
#   Затем запусти этот bootstrap НА СЕРВЕРЕ.
#
# ПЕРЕД запуском убедись:
#   - DNS novirell.shop -> IP этого сервера (проверено: A-запись уже указывает);
#   - порты 80/443 обслуживает mas-nginx (mail-agregator);
#   - код sms уже доставлен в /opt/sms-agreagtor (см. выше).
# =============================================================================
set -euo pipefail

APP_DIR="/opt/sms-agreagtor"
DOMAIN="novirell.shop"
EDGE_CONTAINER="mas-nginx"
MAS_TEMPLATES_DIR="/opt/mail-agregator/deploy/nginx/templates"
WEBROOT="/var/www/certbot"
LE_EMAIL="${LE_EMAIL:-}"   # опционально: email для Let's Encrypt account

log() { printf '\n=== %s ===\n' "$*"; }

# --- 1. Предусловия -----------------------------------------------------------
log "Проверка предусловий"
command -v docker >/dev/null || { echo "FATAL: docker не установлен"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "FATAL: docker compose plugin не установлен"; exit 1; }
command -v certbot >/dev/null || { echo "FATAL: certbot не установлен на хосте"; exit 1; }
docker network inspect mas-net >/dev/null 2>&1 || {
  echo "FATAL: внешняя docker-сеть mas-net не найдена (создаётся стеком mail-agregator)"; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$EDGE_CONTAINER" || {
  echo "FATAL: контейнер edge $EDGE_CONTAINER не запущен"; exit 1; }
mkdir -p "$WEBROOT"

# --- 2. Наличие кода и .env ---------------------------------------------------
log "Проверка доставленного кода в $APP_DIR"
if [[ ! -f "$APP_DIR/deploy/deploy.sh" || ! -f "$APP_DIR/docker-compose.prod.yml" ]]; then
  echo "FATAL: код sms не найден в $APP_DIR."
  echo ">>> Сначала доставь дерево репозитория архивом (git на сервере не нужен), напр.:"
  echo ">>>   tar czf - --exclude=.git --exclude=.env --exclude=secrets . \\"
  echo ">>>     | ssh ${SSH_USER:-root}@<host> 'mkdir -p $APP_DIR && tar xzf - -C $APP_DIR'"
  exit 1
fi
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  cp .env.prod.example .env
  echo ">>> Создан $APP_DIR/.env из .env.prod.example."
  echo ">>> ЗАПОЛНИ реальные секреты (POSTGRES_PASSWORD, ADMIN_PASSWORD, TELEGRAM_BOT_TOKEN,"
  echo ">>> TWILIO_*, DATABASE_URL с тем же паролем) и перезапусти bootstrap."
  echo ">>> Файл .env в .gitignore — в репозиторий не попадёт."
  exit 2
fi

# --- 3. Выпуск cert novirell.shop (chicken-and-egg через HTTP-only vhost) -----
CONF_PATH_IN_EDGE="/etc/nginx/conf.d/${DOMAIN}.conf"

if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
  log "Cert $DOMAIN уже существует — пропускаю выпуск"
else
  log "Выпуск cert $DOMAIN: временный HTTP-only vhost в mas-nginx"
  TMP_HTTP_CONF="$(mktemp)"
  cat > "$TMP_HTTP_CONF" <<EOF
server {
    listen      80;
    listen      [::]:80;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type "text/plain";
        try_files \$uri =404;
    }
    location / { return 404; }
}
EOF
  docker cp "$TMP_HTTP_CONF" "${EDGE_CONTAINER}:${CONF_PATH_IN_EDGE}"
  rm -f "$TMP_HTTP_CONF"
  docker exec "$EDGE_CONTAINER" nginx -t
  docker exec "$EDGE_CONTAINER" nginx -s reload

  log "certbot certonly --webroot (тот же механизм, что postapp.store)"
  EMAIL_ARGS=(--register-unsafely-without-email)
  [[ -n "$LE_EMAIL" ]] && EMAIL_ARGS=(-m "$LE_EMAIL")
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
    --non-interactive --agree-tos --keep-until-expiring \
    --cert-name "$DOMAIN" "${EMAIL_ARGS[@]}"
fi

# --- 4. Полный edge-vhost (HTTP+HTTPS) ---------------------------------------
log "Установка полного edge-vhost $DOMAIN"
VHOST_SRC="$APP_DIR/deploy/nginx/${DOMAIN}.conf"
[[ -f "$VHOST_SRC" ]] || { echo "FATAL: не найден $VHOST_SRC"; exit 1; }

# Primary (envsubst-free): статический .conf прямо в conf.d текущего контейнера —
# его грузит `include /etc/nginx/conf.d/*.conf` из nginx.conf. НЕ проходит через
# их envsubst, применяется немедленно.
docker cp "$VHOST_SRC" "${EDGE_CONTAINER}:${CONF_PATH_IN_EDGE}"
docker exec "$EDGE_CONTAINER" nginx -t
docker exec "$EDGE_CONTAINER" nginx -s reload

# Durability across recreate: копия как .template в templates dir. При старте
# mas-nginx alpine-entrypoint регенерирует conf.d из templates. envsubst идёт по
# ЯВНОМУ allowlist (defined_envs из env контейнера: SERVER_DOMAIN и служебные
# NGINX_*/PATH/*_RELEASE); наши nginx-переменные ($host/$uri/$remote_addr/
# $proxy_add_x_forwarded_for/$sms_upstream) в allowlist НЕ входят -> сохраняются
# (envsubst-safe, тот же приём, что в их default.conf.template). Self-heal: после
# их `git clean -fdx` повторный запуск sms-деплоя восстановит и template, и conf.d.
if [[ -d "$MAS_TEMPLATES_DIR" ]]; then
  cp "$VHOST_SRC" "${MAS_TEMPLATES_DIR}/${DOMAIN}.conf.template"
  echo "template установлен (durability): ${MAS_TEMPLATES_DIR}/${DOMAIN}.conf.template"
else
  echo "WARN: $MAS_TEMPLATES_DIR не найден — durability across mas-nginx recreate не гарантирована"
fi

# --- 5. Первый запуск стека sms ----------------------------------------------
log "Первый запуск стека sms (build + migrate + up)"
bash deploy/deploy.sh

log "Bootstrap завершён. Дальнейшие обновления — через CD (push в main)."
