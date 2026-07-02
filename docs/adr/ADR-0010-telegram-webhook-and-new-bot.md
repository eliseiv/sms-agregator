# ADR-0010 — Telegram webhook (`/start`) и новый бот

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-02 |
| Связано | ADR-0004, ADR-0005 (long polling удалён), ADR-0007; [03-architecture.md](../03-architecture.md), [05-api-contracts.md](../05-api-contracts.md) §3a, [07-deployment.md](../07-deployment.md), [08-security.md](../08-security.md) §11 |

## Context

По [ADR-0005](./ADR-0005-sms-addressing-via-team.md) long polling и `/start`-подписка удалены; бот больше не «подписывает» получателей. Но пользователю всё ещё нужен вход в сервис: при команде `/start` бот должен прислать кнопку открытия Mini App (`web_app`), а весь функционал — внутри Mini App ([ADR-0004](./ADR-0004-telegram-mini-app-sso.md)). Нужен способ принимать апдейты бота **без polling**.

Дополнительно проект переходит на **нового бота**: новый `TELEGRAM_BOT_TOKEN` заменяет старый — он используется и для HMAC-валидации initData (`POST /api/telegram/auth`, [08](../08-security.md) §5), и для `sendMessage`/доставки SMS.

## Decision

1. **Приём апдейтов — через webhook, не polling.** Новый endpoint `POST /api/telegram/webhook` ([05](../05-api-contracts.md) §3a). Валидация секрета по заголовку `X-Telegram-Bot-Api-Secret-Token == TELEGRAM_WEBHOOK_SECRET` (несовпадение/отсутствие → `403`). CSRF-exempt (нет сессии; защита — секрет-токен). Rate-limit по IP. Чувствительное (тело апдейта, токены) не логируется.
2. **Бот обрабатывает ТОЛЬКО `/start`.** На `message.text == "/start"` → `sendMessage` с кнопкой `web_app` (url = `TELEGRAM_WEBAPP_URL` = `https://novirell.shop`) и приветствием. Никаких других команд (`/help` и пр.) не вводится — весь функционал в Mini App. Прочие апдейты → `200 OK` без действий.
3. **Меню команд — только `/start` (или пусто).** На деплое одноразово вызывается `setMyCommands` со списком, содержащим только `/start` (либо пустым). Операция — [07-deployment.md](../07-deployment.md).
4. **`setWebhook` — одноразовый шаг деплоя.** Telegram Bot API `setWebhook` с `url = https://novirell.shop/api/telegram/webhook` и `secret_token = TELEGRAM_WEBHOOK_SECRET`. Операция — [07-deployment.md](../07-deployment.md).
5. **Новый бот и env.** `TELEGRAM_BOT_TOKEN` заменяется новым токеном (заменяет старый и для HMAC initData, и для `sendMessage`). Новый секрет `TELEGRAM_WEBHOOK_SECRET` (случайная строка ≥32 символов) — только через env/secret manager ([08](../08-security.md) §9).
6. **Manual-предпосылка (вне кода):** в @BotFather у нового бота должен быть задан домен Mini App — `novirell.shop`. Фиксируется как ручной шаг деплоя ([07](../07-deployment.md)).

## Rationale

- **Webhook вместо polling** согласуется с уже принятым удалением long polling ([ADR-0005](./ADR-0005-sms-addressing-via-team.md)): сервис за HTTPS-edge ([ADR-0007](./ADR-0007-deploy-behind-shared-edge-nginx.md)) уже принимает публичные POST (Twilio) — добавить ещё один защищённый endpoint дешевле и надёжнее фонового polling-loop.
- **Секрет-токен в заголовке** — рекомендованный Telegram механизм аутентификации webhook: простой constant-check без парсинга payload, отсекает поддельные вызовы до обработки.
- **Только `/start`** — по требованию владельца: единственная функция бота — привести пользователя в Mini App; отсутствие прочих команд убирает поверхность для ошибок и лишнюю логику.
- **Единый токен нового бота** для initData-HMAC и sendMessage сохраняет модель [ADR-0004](./ADR-0004-telegram-mini-app-sso.md)/[ADR-0005](./ADR-0005-sms-addressing-via-team.md) (один бот на вход и доставку) — меняется только значение токена.

## Consequences

**Плюсы:** нет фонового polling; аутентифицированный публичный endpoint; минимальная логика бота; согласовано с edge-nginx-топологией.

**Минусы / издержки:**
- Требуются одноразовые операции деплоя (`setWebhook`, `setMyCommands`, домен Mini App в @BotFather) — вне CD-цикла ([07](../07-deployment.md)).
- Смена бот-токена инвалидирует старые initData и требует пере-настройки webhook на новом боте (ожидаемо при замене бота).
- Публичный `POST /api/telegram/webhook` — новая поверхность атаки; митигирована секрет-токеном + rate-limit + отказом логировать payload ([08](../08-security.md) §11).

## Alternatives

1. **Вернуть long polling.** Отклонено: противоречит [ADR-0005](./ADR-0005-sms-addressing-via-team.md); лишний фоновый loop; хуже за edge-nginx.
2. **Не обрабатывать `/start` вовсе (только Mini App-ссылка в BotFather).** Отклонено: пользователю удобнее получить кнопку `web_app` прямо в чате после `/start`; это ожидаемый UX-паттерн (референс mail-agregator).
3. **Webhook без секрет-токена, полагаясь только на «секретный» путь.** Отклонено: путь легко угадать/утечь; секрет-токен в заголовке — стандартная защита.
4. **Оставить старый бот-токен.** Отклонено: требование владельца — новый бот.
