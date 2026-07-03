# ADR-0013 — On-demand синхронизация номеров из Twilio (как unassigned)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-03 |
| Связано | ADR-0009 (unassigned-пул + распределение — **переиспользует**), ADR-0005; [03-architecture.md](../03-architecture.md) §«Что удаляется» (контекст: авто-sync-loop удалён), [04-data-model.md](../04-data-model.md), [05-api-contracts.md](../05-api-contracts.md) §4a,§7, [06-testing-strategy.md](../06-testing-strategy.md), [07-deployment.md](../07-deployment.md) §«Удаляемые» |

## Context

Приём SMS работает (`POST /api/webhooks/twilio/sms` → `handle_incoming_sms`). Но номера, на которые Twilio шлёт SMS, могут отсутствовать в нашей `phone_numbers`. Тогда `find_by_phone(to)` не находит номер → `team_id = NULL` → ветка «неизвестный номер» → SMS сохраняется, но **не доставляется** (0 получателей, warning в лог). Чтобы номера доставляли, они должны присутствовать в `phone_numbers` и быть распределены по командам.

Ранее существовал фоновой авто-sync номеров из Twilio (`twilio_numbers_sync_loop`, env `TWILIO_NUMBERS_SYNC_ENABLED`/`TWILIO_NUMBERS_SYNC_INTERVAL_SECONDS`). Он **удалён** при переходе на новую модель ([03-architecture.md](../03-architecture.md) §«Что удаляется», [07-deployment.md](../07-deployment.md) §«Удаляемые»): фоновой цикл усложнял процесс и авто-привязывал номера, что несовместимо с моделью распределения через админа ([ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md)).

Требование владельца: уметь **по требованию** (не авто, не по расписанию) подтянуть входящие номера Twilio-аккаунта в нашу БД **как unassigned**, чтобы super_admin распределял их по командам вручную через `/admin` ([ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md)). Источник данных — Twilio Account (IncomingPhoneNumbers), а не легаси-SQLite (для SQLite есть отдельный `scripts/import_numbers.py`, [ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md) §7).

## Decision

1. **On-demand endpoint `POST /api/admin/numbers/sync`** (`require_admin`, только super_admin). Тянет **все** входящие номера Twilio-аккаунта через Twilio API (аутентификация `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` из конфига — те же секреты, что для проверки подписи вебхука), проходя **все страницы** (пагинация). Каждый номер (E.164) нормализуется через `normalize_phone` и upsert'ится в `phone_numbers` как **unassigned** (`team_id = NULL`, `added_by_user_id = NULL`) идемпотентно: `INSERT ... ON CONFLICT (phone_number) DO NOTHING`. Контракт — [05-api-contracts.md](../05-api-contracts.md) §4a.

2. **Никакого авто-назначения.** Новые номера появляются только в unassigned-пуле; их распределение по командам — по-прежнему ручное через `PATCH /api/admin/numbers/{id}` ([ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md) §4). Это осознанное отличие от удалённого `twilio_numbers_sync_loop` (тот привязывал номера сам).

3. **Идемпотентность и неприкосновенность существующих.** `ON CONFLICT (phone_number) DO NOTHING` гарантирует: уже существующие номера — в т.ч. **назначенные командам** (`team_id IS NOT NULL`) и unassigned — **не трогаются** (ни `team_id`, ни `label`, ни `added_by_user_id`). Повторный sync безопасен, добавляет только новые. Ответ несёт счётчики `{synced_total, added, skipped_existing}`.

4. **Отказоустойчивость.** Сбой Twilio API (сетевой, аутентификация, 5xx от Twilio, таймаут) → `502/503 {"error":"twilio_error"}`; частичной записи не происходит вне уже вставленных строк (каждая вставка идемпотентна, поэтому даже прерывание безопасно — повтор дособерёт). Отсутствие сконфигурированных `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` → `503 {"error":"twilio_not_configured"}`.

5. **CLI-скрипт `scripts/sync_twilio_numbers.py`** (one-off) — тот же механизм (тот же Twilio-клиент + upsert), для запуска на сервере вне HTTP. Идемпотентен, печатает те же счётчики. Эксплуатация — [07-deployment.md](../07-deployment.md).

6. **Аудит.** Действие `numbers_synced` добавляется в enum `admin_audit.action` ([04-data-model.md](../04-data-model.md)); `details = {synced_total, added, skipped_existing}`.

7. **UI.** В секции номеров на `/admin` — кнопка «Синхронизировать из Twilio» → `POST /api/admin/numbers/sync` → показать результат (`added`/`skipped_existing`) → обновить список unassigned. Контракт UI — [05-api-contracts.md](../05-api-contracts.md) §7.

8. **Схема БД не меняется** (кроме enum-строки аудита). Поле `source`/`provider` у `phone_numbers` **не вводится**: источник номера различается по факту наличия, отдельная колонка провайдера в текущей модели не требуется. Twilio-клиент официального SDK — **синхронный**; способ вызова из async-хендлера (threadpool/`run_in_executor`) — на усмотрение backend (сверить с кодом проекта), ADR его не диктует.

## Rationale

- **Переиспользование unassigned-пула** ([ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md)): sync — это просто ещё один источник наполнения пула (наряду с `import_numbers.py` из SQLite и ручным `POST /api/numbers`). Никаких новых веток в SMS-пайплайне.
- **On-demand вместо фонового цикла**: явный триггер админом проще, наблюдаемее и дешевле фонового loop'а; не держит открытых соединений/таймеров, не авто-привязывает (что и было причиной удаления старого loop).
- **`ON CONFLICT DO NOTHING` по естественному ключу `phone_number`** — единый идемпотентный примитив, что и у `import_numbers.py`; гарантирует неприкосновенность назначенных номеров.
- **Один механизм для HTTP и CLI**: endpoint и скрипт делят реализацию (сервис-функция), исключая расхождение поведения.
- **Секреты уже есть**: `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` уже в конфиге для проверки подписи вебхука — новых env не требуется.

## Consequences

**Плюсы:** номера из Twilio попадают в БД без ручного ввода; распределение остаётся под контролем админа; идемпотентно и безопасно для назначенных номеров; нет фонового цикла; переиспользован существующий unassigned-пул и код `import_numbers`-подобного upsert'а.

**Минусы / издержки:**
- Sync тянет **все** номера аккаунта каждый вызов (полная пагинация) — приемлемо при объёме ~сотни номеров; при кратном росте аккаунта возможна оптимизация (инкрементальный sync) — вне scope, зафиксировать в tech-debt при необходимости.
- Синхронный Twilio SDK в async-приложении требует вызова через threadpool, иначе блокирует event loop (backend обязан вынести в executor).
- После sync номера всё ещё не доставляют SMS, пока админ не распределит их по командам (ожидаемо — как и весь unassigned-пул).

## Alternatives

1. **Вернуть фоновой авто-sync-loop.** Отклонено: причина удаления (сложность + авто-привязка, несовместимая с ADR-0009) сохраняется; on-demand решает задачу без цикла.
2. **Sync с авто-назначением на команды.** Отклонено: нарушает модель [ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md) (распределение — привилегия админа); неясно, какой команде принадлежит новый номер Twilio.
3. **Только CLI-скрипт, без endpoint.** Отклонено: админу нужен self-service из `/admin` без доступа к серверу; CLI оставлен как дополнительный one-off путь.
4. **Колонка `phone_numbers.source`/`provider`.** Отклонено: не требуется для текущих сценариев (различаем по факту наличия), лишнее усложнение схемы.
