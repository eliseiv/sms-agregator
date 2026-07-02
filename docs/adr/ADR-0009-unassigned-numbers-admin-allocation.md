# ADR-0009 — Unassigned-номера и админское распределение по командам

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-02 |
| Связано | ADR-0003, ADR-0005 (**частично амендит** §Decision 1), ADR-0006; [03-architecture.md](../03-architecture.md), [04-data-model.md](../04-data-model.md), [05-api-contracts.md](../05-api-contracts.md) §4a,§6, [06-testing-strategy.md](../06-testing-strategy.md), [07-deployment.md](../07-deployment.md) |

## Context

По [ADR-0005](./ADR-0005-sms-addressing-via-team.md) §1 номер жёстко привязан к команде: `phone_numbers.team_id NOT NULL FK teams(id) ON DELETE CASCADE`. Возникли два требования владельца, несовместимых с этим ограничением:

1. **Пул нераспределённых номеров.** Номер может существовать в системе **без команды** (unassigned), а super_admin распределяет его по командам позже. Это нужно для одноразового импорта ~328 номеров из старой SQLite (`data/service.db`, `twilio_numbers`), которые на момент импорта не привязаны ни к какой команде.
2. **Сохранение номеров при расформировании команды.** Удаление команды не должно уничтожать её номера (`ON DELETE CASCADE` терял бы номера) — они должны становиться unassigned и оставаться в пуле для перераспределения.

При этом должен сохраниться существующий путь: **любой участник** добавляет номер в **свою** команду через `POST /api/numbers` ([ADR-0005](./ADR-0005-sms-addressing-via-team.md) §5). Оба пути сосуществуют.

## Decision

1. **`phone_numbers.team_id` становится NULLABLE.** `unassigned = team_id IS NULL`. FK → `teams(id)` меняется с `ON DELETE CASCADE` на **`ON DELETE SET NULL`**. Это **амендит** [ADR-0005](./ADR-0005-sms-addressing-via-team.md) §Decision 1 (в остальном ADR-0005 в силе). Схема и индексы — [04-data-model.md](../04-data-model.md) §`phone_numbers`.
2. **Удаление команды → её номера становятся unassigned** (`team_id → NULL` через `ON DELETE SET NULL`), не удаляются. Контракт `DELETE /api/admin/teams/{id}` обновлён ([05](../05-api-contracts.md) §5): номера сохраняются в пуле.
3. **Согласованность SMS-пайплайна.** SMS на unassigned-номер (номер существует, `team_id IS NULL`) обрабатывается **идентично неизвестному номеру**: `inbound_sms.team_id = phone.team_id = NULL` → ветка `team_id IS NULL` → получателей нет, `inbound_sms` сохраняется, webhook `200`. Отдельной ветки не вводится; `recipients_for_team` для unassigned-номера **не** вызывается. Детали — [03-architecture.md](../03-architecture.md) §«Поток приёма».
4. **Админские контракты** (`require_admin`, только super_admin):
   - `GET /api/admin/numbers` — список **всех** номеров с фильтром `?assignment=assigned|unassigned|all` и `?team_id=<id>`; поля включают `team_id`/`team_name` (null для unassigned).
   - `PATCH /api/admin/numbers/{id}` `{team_id: <id>|null}` — назначение/переназначение/снятие команды. `team_id=null` → снять (сделать unassigned). Коды ошибок: `number_not_found` (404), `team_not_found` (404). Audit `number_team_assigned`.
5. **Права.** Участник (`group_member`/`group_leader`) добавляет/удаляет номера только своей команды (`POST/DELETE /api/numbers`, без изменений). Назначение **произвольной** команды и распределение пула — только `super_admin` через `PATCH /api/admin/numbers/{id}`. Участник unassigned-пул не видит и не распределяет.
6. **UI.** На `/admin` добавляется секция распределения unassigned-номеров (список пула + выбор команды на номер). Контракт UI — [05](../05-api-contracts.md) §7.
7. **Импорт (одноразовый, идемпотентный).** Отдельный скрипт-режим `scripts/import_numbers.py` (backend реализует) переносит номера из старой SQLite `twilio_numbers` (`phone_number`, `label`, `is_active`) в `phone_numbers` как **unassigned** (`team_id = NULL`, `added_by_user_id = NULL`). Идемпотентность — `INSERT ... ON CONFLICT (phone_number) DO NOTHING`. Переносятся **только номера** — projects/teams/users/deliveries НЕ переносятся (это зона [ADR-0006](./ADR-0006-data-migration-sqlite-to-pg.md); данный импорт от него независим). Эксплуатация — [07-deployment.md](../07-deployment.md).

## Rationale

- **NULLABLE + SET NULL** — минимальное изменение схемы, покрывающее оба требования (пул + сохранение при удалении команды) без новых таблиц.
- Переиспользование ветки `team_id IS NULL` для unassigned-номера не добавляет спец-кейсов в пайплайн: «нет команды → нет получателей» — единый инвариант и для неизвестного, и для нераспределённого номера.
- **Отдельный скрипт импорта** (а не расширение `migrate_sqlite_to_pg.py`) изолирует «импорт только номеров как unassigned» от полной миграции истории — их запускают в разных сценариях, у них разные инварианты. `ON CONFLICT DO NOTHING` по естественному ключу `phone_number` делает повтор безопасным.
- **Разделение прав** сохраняет модель [ADR-0005](./ADR-0005-sms-addressing-via-team.md): участник управляет только своей командой; кросс-командное распределение — привилегия админа.

## Consequences

**Плюсы:** пул нераспределённых номеров; номера переживают удаление команды; импорт легаси-номеров без переноса остальной модели; пайплайн SMS не усложняется.

**Минусы / издержки:**
- Инвариант «у номера всегда есть команда» снят — код и запросы обязаны учитывать `team_id IS NULL` (unassigned SMS → 0 получателей; UI-фильтры).
- Unassigned-номер молча не доставляет SMS до назначения команды (ожидаемо; warning в лог, как для неизвестного номера).
- `GET /api/numbers` для super_admin теперь может вернуть номера с `team_name=null`; каноничный админский список — `GET /api/admin/numbers`.

## Alternatives

1. **Отдельная таблица `unassigned_numbers`.** Отклонено: дублирует структуру `phone_numbers`, требует переноса строк при назначении, усложняет уникальность `phone_number`.
2. **Служебная команда «Unassigned» (`team_id` на техкоманду).** Отклонено: ломает семантику (техкоманда была бы получателем SMS/имела бы лидера по инвариантам ролей); `NULL` честнее выражает «нет команды».
3. **Оставить `ON DELETE CASCADE`, номера удалять с командой.** Отклонено: требование — сохранять номера в пуле.
4. **Расширить `migrate_sqlite_to_pg.py` режимом импорта.** Отклонено: смешивает два несовместимых сценария (полная миграция vs импорт-только-номеров-unassigned) в одном скрипте.
