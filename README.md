# Twilio SMS Telegram Bot

Сервис принимает входящие SMS от Twilio, сохраняет их в SQLite, определяет проект по номеру получателя и рассылает сообщения в Telegram пользователям с доступом к проекту.

## Что внутри

- `FastAPI` backend
- DDD-структура: `domain / application / infrastructure / api`
- `SQLite` для MVP
- Telegram long polling без отдельного webhook
- Проверка подписи `Twilio`
- Docker + gunicorn

## Основные маршруты

- `GET /health`
- `POST /api/webhooks/twilio/sms`
- `GET /api/admin/projects`
- `POST /api/admin/projects`
- `GET /api/admin/numbers`
- `POST /api/admin/numbers`
- `GET /api/admin/users`
- `POST /api/admin/users`
- `POST /api/admin/users/{telegram_id}/projects`
- `GET /api/admin/messages`
- `POST /api/admin/deliveries/retry`

Все `/api/admin/*` защищены `Basic Auth`.
Резервно можно оставить `X-Admin-Token`, если он настроен в env.

## Telegram команды

- `/start`
- `/my_projects`
- `/numbers`

## Локальный запуск

```powershell
cd twilio-sms-telegram-bot
copy .env.example .env
docker compose up -d --build
```

## Как добавить проект, номер и пользователя

### 1. Создать проект

```bash
curl -X POST 'http://localhost:8137/api/admin/projects' \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: change-me-admin-token' \
  -d '{
    "name": "Проект A",
    "description": "Основной номер регистрации"
  }'
```

### 2. Добавить номер Twilio

```bash
curl -X POST 'http://localhost:8137/api/admin/numbers' \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: change-me-admin-token' \
  -d '{
    "phone_number": "+1234567890",
    "project_id": 1,
    "label": "US main"
  }'
```

### 3. Добавить Telegram-пользователя и дать доступ

```bash
curl -X POST 'http://localhost:8137/api/admin/users' \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: change-me-admin-token' \
  -d '{
    "telegram_id": 123456789,
    "username": "operator_one",
    "project_ids": [1]
  }'
```

## Формат сообщения

```text
📩 Новое SMS

📱 Номер: +1234567890
👤 От: +1987654321
💬 Текст: Your code is 1234
🕒 Время: 31.03 14:22
```

## Деплой

Сервис рассчитан на запуск на сервере под доменом `twiliosms.webberapp.shop`.
