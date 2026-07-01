# 99. Open Questions

Открытые вопросы. Формат `Q-<AREA>-<N>`. Закрытие — решением или новым ADR.

| ID | Вопрос | Статус | Влияние | Предложение по умолчанию |
| --- | --- | --- | --- | --- |
| `Q-TECH-1` | Формат манифеста зависимостей: `requirements.txt` (текущий) или `pyproject.toml` (референс mail-agregator)? | open | Низкое | Сохранить `requirements.txt`, добавить новые зависимости туда; миграция на `pyproject.toml` — отдельной задачей. |
| `Q-DATA-1` | Сохранять ли `projects.description` при миграции (добавить `teams.description`)? | open | Низкое | Не сохранять — в текущем UI поле не используется. Если понадобится — добавить nullable-колонку миграцией. |
| `Q-AUTH-1` | Политика пароля для `/set-password` сверх «≥8 символов и совпадение»: нужны ли классы символов/blacklist? | open | Среднее | Минимум 8 символов + совпадение подтверждения; без обязательных классов на MVP. |
| `Q-SEC-1` | Точные значения rate-limit для `/login`, `/login/password`, `/set-password` (в 08-security указаны рекомендованные ~10/min). | open | Низкое | Использовать 10/min per IP + per username до пересмотра. |
| `Q-TG-1` | Нужен ли `super_admin` быть получателем SMS (у него `team_id IS NULL` → не входит ни в одну команду)? | open | Среднее | Нет: super_admin не получает SMS (не привязан к команде). Если нужно — заводить его как участника отдельной команды. |
| `Q-CSP-1` | Точный `frame-ancestors`/`X-Frame-Options` под Telegram Mini App и `TELEGRAM_WEBAPP_URL`. | open | Низкое | `frame-ancestors https://telegram.org`; уточнить при интеграции реального Mini App. |
