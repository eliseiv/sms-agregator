#!/usr/bin/env bash
# =============================================================================
# deploy/deploy.sh — идемпотентный деплой sms-agreagtor на прод-сервер.
#
# Вызывается workflow'ом Deploy по SSH ПОСЛЕ доставки рабочего дерева архивом
# (tar over SSH; git на сервере НЕ используется). Собирает образ НА СЕРВЕРЕ (без
# registry), применяет миграции, поднимает стек, ждёт healthcheck, переустанавливает
# edge-vhost novirell.shop в mas-nginx. При провале healthcheck — откат образа
# app на предыдущий.
#
# Требует на сервере: docker + compose plugin, заполненный ./.env,
# существующую внешнюю сеть mas-net, выпущенный cert novirell.shop
# (всё это делает однократный deploy/bootstrap.sh).
#
# ВСЕ операции additive и idempotent; mail-agregator (postapp.store) не трогаем.
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

COMPOSE=(docker compose -f docker-compose.prod.yml)
APP_IMAGE="sms-aggregator-app:latest"
ROLLBACK_IMAGE="sms-aggregator-app:rollback"
HEALTH_URL="http://127.0.0.1:8137/health"
EDGE_CONTAINER="mas-nginx"
MAS_TEMPLATES_DIR="/opt/mail-agregator/deploy/nginx/templates"
VHOST_SRC="$APP_DIR/deploy/nginx/novirell.shop.conf"
DOMAIN="novirell.shop"

log() { printf '\n=== %s ===\n' "$*"; }

# --- 0. Предусловия -----------------------------------------------------------
[[ -f .env ]] || { echo "FATAL: .env отсутствует в $APP_DIR (см. deploy/README.md)"; exit 1; }
docker network inspect mas-net >/dev/null 2>&1 || {
  echo "FATAL: docker-сеть mas-net не найдена — сначала запусти deploy/bootstrap.sh"; exit 1; }

# --- 1. Сохранить текущий образ app для отката --------------------------------
log "Снимок текущего образа app для отката"
PREV_IMAGE_ID="$(docker images -q "$APP_IMAGE" 2>/dev/null || true)"
if [[ -n "$PREV_IMAGE_ID" ]]; then
  docker image tag "$APP_IMAGE" "$ROLLBACK_IMAGE"
  echo "rollback-точка: $PREV_IMAGE_ID"
else
  echo "предыдущего образа нет (первый деплой) — откат образа будет недоступен"
fi

# --- 2. Сборка образа на сервере ---------------------------------------------
log "docker compose build"
"${COMPOSE[@]}" build

# --- 3. Миграции (до старта нового кода) --------------------------------------
log "Применение миграций (alembic upgrade head)"
"${COMPOSE[@]}" up -d postgres redis
"${COMPOSE[@]}" run --rm migrate

# --- 4. Поднять стек ----------------------------------------------------------
log "docker compose up -d"
"${COMPOSE[@]}" up -d

# --- 5. Healthcheck с ожиданием ----------------------------------------------
log "Ожидание healthcheck: $HEALTH_URL"
healthy=0
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$HEALTH_URL" || true)"
  if [[ "$code" == "200" ]]; then healthy=1; echo "health OK (попытка $i)"; break; fi
  echo "  ... ещё не готово (код=$code), попытка $i/30"; sleep 3
done

if [[ "$healthy" -ne 1 ]]; then
  log "HEALTHCHECK ПРОВАЛЕН — откат образа app"
  if [[ -n "$PREV_IMAGE_ID" ]]; then
    docker image tag "$ROLLBACK_IMAGE" "$APP_IMAGE"
    "${COMPOSE[@]}" up -d app
    echo "Откат образа app выполнен. ВНИМАНИЕ: миграции БД forward-only —"
    echo "если требуется откат схемы, выполни вручную: ${COMPOSE[*]} run --rm migrate alembic downgrade -1"
  else
    echo "Откат образа невозможен (нет предыдущего)."
  fi
  exit 1
fi

# --- 6. Переустановка edge-vhost novirell.shop (idempotent) -------------------
log "Переустановка edge-vhost $DOMAIN в $EDGE_CONTAINER"
if ! docker ps --format '{{.Names}}' | grep -qx "$EDGE_CONTAINER"; then
  echo "WARN: контейнер $EDGE_CONTAINER не запущен — пропускаю установку vhost."
elif [[ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
  echo "WARN: cert /etc/letsencrypt/live/${DOMAIN}/ отсутствует — сначала deploy/bootstrap.sh (выпуск cert). Пропускаю vhost."
else
  # Primary (envsubst-free): статический .conf прямо в conf.d текущего контейнера.
  # Грузится через `include /etc/nginx/conf.d/*.conf`; их envsubst НЕ применяется.
  docker cp "$VHOST_SRC" "${EDGE_CONTAINER}:/etc/nginx/conf.d/${DOMAIN}.conf"
  if docker exec "$EDGE_CONTAINER" nginx -t; then
    docker exec "$EDGE_CONTAINER" nginx -s reload
    echo "edge-vhost применён (conf.d), mas-nginx reloaded."
    # Durability across recreate: копия как .template (envsubst-safe — наши
    # nginx-переменные не входят в allowlist defined_envs mas-nginx; см.
    # deploy/README.md). Self-heal: повторный деплой восстановит после git clean.
    if [[ -d "$MAS_TEMPLATES_DIR" ]]; then
      cp "$VHOST_SRC" "${MAS_TEMPLATES_DIR}/${DOMAIN}.conf.template"
      echo "template установлен (durability): ${MAS_TEMPLATES_DIR}/${DOMAIN}.conf.template"
    else
      echo "WARN: $MAS_TEMPLATES_DIR не найден — durability across recreate не гарантирована."
    fi
  else
    echo "FATAL: nginx -t не прошёл — откатываю vhost, чтобы не уронить postapp.store"
    docker exec "$EDGE_CONTAINER" rm -f "/etc/nginx/conf.d/${DOMAIN}.conf" || true
    [[ -d "$MAS_TEMPLATES_DIR" ]] && rm -f "${MAS_TEMPLATES_DIR}/${DOMAIN}.conf.template" || true
    docker exec "$EDGE_CONTAINER" nginx -s reload || true
    exit 1
  fi
fi

# --- 7. Финальная проверка снаружи (best-effort) -----------------------------
log "Внешняя проверка https://${DOMAIN}/health (best-effort)"
ext="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "https://${DOMAIN}/health" || true)"
echo "https://${DOMAIN}/health -> $ext"

# --- 8. Уборка ----------------------------------------------------------------
docker image prune -f >/dev/null 2>&1 || true
log "Деплой завершён успешно."
