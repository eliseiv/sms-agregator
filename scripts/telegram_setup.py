"""One-off деплой-операции Telegram-бота (ADR-0010, docs/07 §Одноразовые операции).

Регистрирует webhook с секрет-токеном и меню команд (только ``/start``). Токен и
секрет берутся из настроек (env), не логируются. Запуск после того, как ``app``
доступен по ``https://novirell.shop``::

    python -m scripts.telegram_setup --action set-webhook
    python -m scripts.telegram_setup --action set-commands
    python -m scripts.telegram_setup --action all
"""

from __future__ import annotations

import argparse
import asyncio

from app.infrastructure.telegram_api import TelegramApiClient
from shared.config import get_settings

_START_COMMANDS = [{"command": "start", "description": "Открыть приложение"}]


async def _run(action: str) -> None:
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в окружении")
    client = TelegramApiClient(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_PROXY_URL)

    if action in ("set-webhook", "all"):
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            raise SystemExit("TELEGRAM_WEBHOOK_SECRET не задан в окружении")
        webhook_url = f"{settings.public_base_url}/api/telegram/webhook"
        await client.set_webhook(
            url=webhook_url, secret_token=settings.TELEGRAM_WEBHOOK_SECRET
        )
        print(f"setWebhook: OK ({webhook_url})")

    if action in ("set-commands", "all"):
        await client.set_my_commands(_START_COMMANDS)
        print("setMyCommands: OK (только /start)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Настройка Telegram webhook/меню (ADR-0010)"
    )
    parser.add_argument(
        "--action",
        choices=["set-webhook", "set-commands", "all"],
        default="all",
        help="Что выполнить",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.action))


if __name__ == "__main__":
    main()
