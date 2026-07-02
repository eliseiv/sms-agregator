# ADR Index

Реестр архитектурных решений SMS-агрегатора. Статусы: `proposed` / `accepted` / `superseded`.

| ADR | Заголовок | Статус | Дата |
| --- | --- | --- | --- |
| [ADR-0001](./ADR-0001-postgres-sqlalchemy-async.md) | Переход на PostgreSQL + SQLAlchemy 2.0 async + Alembic + Redis | accepted | 2026-07-01 |
| [ADR-0002](./ADR-0002-two-step-login.md) | Двухэтапный логин (логин → пароль) | accepted | 2026-07-01 |
| [ADR-0003](./ADR-0003-roles-and-teams.md) | Роли и команды (super_admin/group_leader/group_member, первый=лидер) | accepted | 2026-07-01 |
| [ADR-0004](./ADR-0004-telegram-mini-app-sso.md) | Telegram Mini App SSO (initData HMAC, pending-токены, self-heal, dead-links) | accepted | 2026-07-01 |
| [ADR-0005](./ADR-0005-sms-addressing-via-team.md) | Адресация SMS через team + telegram_links (замена подписки /start) | accepted | 2026-07-01 |
| [ADR-0006](./ADR-0006-data-migration-sqlite-to-pg.md) | Одноразовая миграция данных SQLite → PostgreSQL | accepted | 2026-07-01 |
| [ADR-0007](./ADR-0007-deploy-behind-shared-edge-nginx.md) | Деплой за общим edge-nginx соседнего сервиса (additive vhost, host certbot) | accepted | 2026-07-02 |
| [ADR-0008](./ADR-0008-root-route-and-per-role-landing.md) | Корневой маршрут `/` и per-role landing (`/` диспетчер, `/app` для участников) | accepted | 2026-07-02 |

## Как добавлять ADR

1. Скопировать структуру существующего: Context / Decision / Rationale / Consequences / Alternatives.
2. Присвоить следующий `ADR-NNNN`, добавить строку в таблицу выше.
3. Значимое решение = затрагивает контракты, схему БД, безопасность или структуру.
