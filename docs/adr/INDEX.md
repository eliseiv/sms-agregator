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
| [ADR-0009](./ADR-0009-unassigned-numbers-admin-allocation.md) | Unassigned-номера и админское распределение по командам (амендит ADR-0005 §1) | accepted | 2026-07-02 |
| [ADR-0010](./ADR-0010-telegram-webhook-and-new-bot.md) | Telegram webhook (`/start`) и новый бот (setWebhook/setMyCommands, TELEGRAM_WEBHOOK_SECRET) | accepted | 2026-07-02 |
| [ADR-0011](./ADR-0011-sticky-logout-vs-miniapp-sso.md) | «Залипающий» logout против авто-SSO Mini App (cookie `sms_logged_out`, амендит ADR-0004 §5) | accepted | 2026-07-02 |
| [ADR-0012](./ADR-0012-multi-team-membership.md) | Multi-team: аддитивная M:N `user_teams` (home = `users.team_id`; амендит ADR-0003 §4, ADR-0005 §2; закрывает TD-003) | accepted | 2026-07-03 |
| [ADR-0013](./ADR-0013-on-demand-twilio-number-sync.md) | On-demand синхронизация номеров из Twilio (как unassigned, без авто-назначения; переиспользует ADR-0009-пул) | accepted | 2026-07-03 |
| [ADR-0014](./ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md) | Просмотр SMS по номерам: ролевой доступ по текущей принадлежности номера (`phone_numbers.team_id`, не снимок) + cursor keyset-пагинация | accepted | 2026-07-03 |
| [ADR-0015](./ADR-0015-admin-users-visual-parity-with-mail-agregator.md) | Визуально-структурный паритет `/admin` (список пользователей) с референсом mail-agregator: единая таблица + `<tbody>`-banding + чипы команд; удаление колонки «Telegram» и секций-на-команду | accepted | 2026-07-04 |

## Как добавлять ADR

1. Скопировать структуру существующего: Context / Decision / Rationale / Consequences / Alternatives.
2. Присвоить следующий `ADR-NNNN`, добавить строку в таблицу выше.
3. Значимое решение = затрагивает контракты, схему БД, безопасность или структуру.
