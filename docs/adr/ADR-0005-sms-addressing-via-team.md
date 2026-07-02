# ADR-0005 — Адресация SMS через team + telegram_links (замена подписки /start)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-01 |
| Связано | ADR-0003, ADR-0004; [03-architecture.md](../03-architecture.md), [04-data-model.md](../04-data-model.md), [05-api-contracts.md](../05-api-contracts.md) §1,§6 |
| Амендмент | Изменён [ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md): **(а)** §Decision 1 (`phone_numbers.team_id NOT NULL ... ON DELETE CASCADE`) — `team_id` теперь NULLABLE, FK `ON DELETE SET NULL` (unassigned-номера); **(б)** следствие в §Consequences/«Минусы» «удаление команды каскадит удаление `phone_numbers`» **более не действует** — удаление команды переводит её номера в unassigned (`team_id → NULL` через `ON DELETE SET NULL`), номера НЕ удаляются. Прочие пункты (получатели, неизвестный номер, идемпотентность, dead-links, удаление long polling, «любой участник добавляет номер») — в силе. |

## Context

Сейчас доступ к номерам и рассылка строятся на `projects` + `user_project_access`, а получатели «подписываются» командой `/start` боту (long polling), при этом `telegram_id` берётся из входящего update. Проблемы: доставка зависит от того, «нажал ли пользователь /start»; нет криптографического доказательства владения Telegram-аккаунтом; long polling и `/start`-хендлеры усложняют сервис; связь «номер → кто получает» неявная (через доступы к проекту).

Новая модель ([ADR-0003](./ADR-0003-roles-and-teams.md)) вводит команды; вход — через Mini App ([ADR-0004](./ADR-0004-telegram-mini-app-sso.md)) с надёжной привязкой `telegram_links`. Нужно определить, кто получает SMS на конкретный номер.

## Decision

1. **Номер привязан к команде.** `phone_numbers.team_id NOT NULL FK teams(id) ON DELETE CASCADE`. Каждый входящий SMS адресуется команде номера-получателя.
2. **Получатели SMS** — все пользователи команды с **активной** привязкой Telegram: `recipients_for_team(team_id)` = `JOIN users ↔ telegram_links` по `users.team_id = :team_id AND telegram_links.dead_at IS NULL`, возвращает пары `(user_id, telegram_user_id)`. chat_id = `telegram_user_id` из initData. Пользователь без живой привязки SMS не получает.
3. **Неизвестный номер** (нет `phone_numbers` для `To`): `inbound_sms` сохраняется с `team_id = NULL`, доставок нет, webhook возвращает `200` (SMS не теряется, warning в лог).
4. **Идемпотентность:**
   - webhook по `twilio_message_sid` (partial-UNIQUE `inbound_sms_sid_uq`) — ретраи Twilio не создают дублей;
   - доставка по `(inbound_sms_id, telegram_user_id)` (UNIQUE `deliveries_sms_chat_uq`) через `try_reserve` (`pg_insert ... on_conflict_do_nothing returning id`) — каждый чат получает не более одного экземпляра.
   - **Восстановимость fan-out (нормативно).** Веерная рассылка обязана быть crash-recoverable: при обрыве процесса в середине fan-out недоставленные получатели добираются без ручного вмешательства. Дедуп-ветка webhook **не** делает ранний возврат — при повторе с тем же `MessageSid` переиспользует `inbound_sms` и снова проходит `recipients_for_team` + `try_reserve` по каждому получателю (доставленные отсекаются идемпотентно, недоставленные досылаются). Эквивалентно допустимо создавать все `pending`-deliveries по снапшоту получателей в одной транзакции до отправки. Недоставленные покрываются либо webhook-retry, либо `delivery_retry_loop`. Детали и тест-требование — [03-architecture.md](../03-architecture.md) §«Восстановимость веерной рассылки», [06-testing-strategy.md](../06-testing-strategy.md).
5. **Добавление номеров — любой участник команды** (`group_member`/`group_leader`) добавляет/удаляет номера **своей** команды (`team_id` из `current_user`); `super_admin` указывает `team_id` явно. Контракты — [05](../05-api-contracts.md) §6.
6. **Удаляется старый flow:** long polling `telegram_polling_loop`, команды `/start`/`/my_projects`/`/numbers`, `handle_telegram_command`, автогенерация проектов/номеров (`_get_or_create_auto_project`, `ensure_all_users_have_project`, `_ensure_number_mapping`, `upsert_user`), а также `twilio_numbers_sync_loop` (для MVP, `TD-001`).
7. **Доставка и dead-links** — как в [ADR-0004](./ADR-0004-telegram-mini-app-sso.md) §5: 403/blocked → `deliveries.status='dead'` + `telegram_links.dead_at`; прочие ошибки → `failed` (retry-loop); успех → `sent`.

## Rationale

- Привязка «номер → команда → участники с живой Telegram-привязкой» делает адресацию явной, детерминированной и проверяемой, в отличие от неявных проектных доступов и «нажал ли /start».
- Требование пользователя: SMS на номер получают все участники команды — точно выражается через `recipients_for_team`.
- Живая `telegram_links` (initData HMAC) гарантирует, что получатель действительно владеет Telegram-аккаунтом; исключает доставку «мёртвым»/чужим chat_id.
- Идемпотентность на двух уровнях защищает от дублей при ретраях Twilio и повторной обработке.
- Удаление long polling и автогенерации сильно упрощает сервис (один бот, HTTPS-endpoint вместо polling).
- «Любой участник добавляет номер» — явное требование; снижает нагрузку на админа.

## Consequences

**Плюсы:** явная и надёжная адресация, отсутствие потерь SMS (неизвестный номер сохраняется), устойчивость к ретраям и блокировкам, упрощённый бот.

**Минусы / издержки:**
- Пользователь получает SMS только после привязки через Mini App — до привязки доставки нет (ожидаемо).
- `super_admin` (без команды) не получает SMS (`Q-TG-1`).
- Номера без автоимпорта из Twilio — добавляются вручную (`TD-001`).
- Удаление номера/команды каскадит (`phone_numbers` при удалении команды) — учитывать в UI/подтверждениях.

## Alternatives

1. **Сохранить `user_project_access` (доступ к проекту как право на приём).** Отклонено: неявно, M:N против single-team-модели, зависит от подписки `/start`.
2. **Оставить `/start`-подписку для получателей.** Отклонено: нет доказательства владения аккаунтом, требует long polling, конфликтует с Mini App SSO.
3. **Номер привязан к конкретным пользователям (M:N number↔user).** Отклонено: усложняет модель; «команда» — естественная единица адресации, совпадает с ролями/видимостью.
4. **Терять SMS на неизвестный номер (404).** Отклонено: потеря данных; сохранение с `team_id=NULL` + `200` безопаснее (можно разобрать позже).
