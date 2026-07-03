# SMS-агрегатор — документация (источник истины)

Сервис приёма входящих SMS от Twilio и рассылки их в Telegram участникам команды, к которой привязан номер. Включает админ-панель со входом через Telegram Mini App, двухэтапную авторизацию и управление командами/пользователями/номерами.

Это единственный источник истины проекта. При расхождении `docs/` ↔ код — исправляется тот, кто не обновил документацию.

## Карта документации

| Файл | Назначение |
| --- | --- |
| [01-overview.md](./01-overview.md) | Назначение, границы, роли, основные сценарии. |
| [02-tech-stack.md](./02-tech-stack.md) | Стек, версии, команды lint/format/type-check, обоснование. |
| [03-architecture.md](./03-architecture.md) | Слои, пакеты, middleware, диаграммы потоков SMS и auth/SSO. |
| [04-data-model.md](./04-data-model.md) | Полная схема PostgreSQL: таблицы, FK, CHECK, индексы, маппинг SQLite→PG. |
| [05-api-contracts.md](./05-api-contracts.md) | Все endpoints: webhook, auth, Mini App SSO, admin, teams, numbers. |
| [06-testing-strategy.md](./06-testing-strategy.md) | Уровни тестов, ключевые сценарии verification. |
| [07-deployment.md](./07-deployment.md) | docker-compose, env-переменные, порядок запуска, миграции. |
| [08-security.md](./08-security.md) | argon2, сессии, CSRF, rate-limit, lockout, HMAC initData, Twilio signature. |
| [99-open-questions.md](./99-open-questions.md) | Открытые вопросы `Q-NNN-N`. |
| [100-known-tech-debt.md](./100-known-tech-debt.md) | Реестр tech-debt `TD-NNN`. |
| [adr/INDEX.md](./adr/INDEX.md) | Реестр архитектурных решений. |

## Статус

Bootstrap-версия документации под крупную доработку (переход на PostgreSQL, админ-панель, Telegram Mini App SSO, привязка номеров к командам). Код ещё не реализован — это ТЗ для backend/frontend/devops/qa.

## Ключевые архитектурные решения (кратко)

- Стек: FastAPI + PostgreSQL 16 + SQLAlchemy 2.0 async (asyncpg) + Alembic + Redis + argon2 (см. [ADR-0001](./adr/ADR-0001-postgres-sqlalchemy-async.md)).
- Двухэтапный логин: сначала логин, затем пароль (см. [ADR-0002](./adr/ADR-0002-two-step-login.md)).
- Роли `super_admin`/`group_leader`/`group_member`, команда = `teams`, первый добавленный участник = лидер (см. [ADR-0003](./adr/ADR-0003-roles-and-teams.md)).
- Вход и восстановление привязки через Telegram Mini App (initData HMAC, pending-токены, self-heal, dead-links) (см. [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md)).
- Адресация SMS: номер → команда → все участники команды (через членство `user_teams`) с живой `telegram_links` (см. [ADR-0005](./adr/ADR-0005-sms-addressing-via-team.md)).
- Миграция данных SQLite → PostgreSQL одноразовым скриптом (см. [ADR-0006](./adr/ADR-0006-data-migration-sqlite-to-pg.md)).
- Production-деплой за общим edge-nginx соседа (`mas-nginx`/`mas-net`), домен novirell.shop, additive vhost, host certbot (см. [ADR-0007](./adr/ADR-0007-deploy-behind-shared-edge-nginx.md)).
- Unassigned-номера: `phone_numbers.team_id` NULLABLE (`ON DELETE SET NULL`), админское распределение по командам, импорт легаси-номеров как unassigned (см. [ADR-0009](./adr/ADR-0009-unassigned-numbers-admin-allocation.md)).
- Приём апдейтов бота через webhook (только `/start` → кнопка Mini App), секрет-токен, новый бот-токен (см. [ADR-0010](./adr/ADR-0010-telegram-webhook-and-new-bot.md)).
- Первый вход по ТЗ: шаг-1 `POST /login` ветвится по состоянию и направляет неактивированный аккаунт сразу на `/set-password` («придумай пароль»); анти-энумерация шага-1 ослаблена до мягкой (амендмент [ADR-0002](./adr/ADR-0002-two-step-login.md), риск TD-010).
- «Залипающий» logout в Mini App: cookie `sms_logged_out` подавляет авто-SSO до явного входа, привязка/push сохраняются (см. [ADR-0011](./adr/ADR-0011-sticky-logout-vs-miniapp-sso.md), амендит [ADR-0004](./adr/ADR-0004-telegram-mini-app-sso.md) §5).
- Multi-team: аддитивная M:N `user_teams`, домашняя команда = `users.team_id`; участник видит/добавляет номера и получает SMS всех своих команд; админ управляет членством; подсветка команд (banding) на `/admin`; роль остаётся глобальной, лидерство — на домашней (см. [ADR-0012](./adr/ADR-0012-multi-team-membership.md), закрывает `TD-003`).
- On-demand sync номеров из Twilio: super_admin наполняет unassigned-пул по кнопке/CLI, `ON CONFLICT DO NOTHING`, без авто-назначения (см. [ADR-0013](./adr/ADR-0013-on-demand-twilio-number-sync.md)).
- Просмотр входящих SMS `GET /messages` (единая страница для всех ролей): участник видит SMS по **текущей** принадлежности номера (`phone_numbers.team_id`), super_admin — все с фильтрами; read-only; первая в проекте cursor keyset-пагинация `(received_at DESC, id DESC)` (см. [ADR-0014](./adr/ADR-0014-sms-viewing-by-number-current-ownership-cursor-pagination.md)).
