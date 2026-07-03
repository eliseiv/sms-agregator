"""Сервис просмотра входящих SMS (docs/05 §9, ADR-0014).

Единая read-only сервис-функция для JSON (``GET /api/messages``) и SSR
(``GET /messages``): ролевая видимость по **текущей** принадлежности номера
(``phone_numbers.team_id``, ADR-0014 §2) + cursor keyset-пагинация.

Слой application не зависит от ``app.api``: принимает роль/scope примитивами
(``is_super_admin``/``team_ids``) и возвращает ORM-строки ``InboundSms`` —
сериализация (``serialize_message``) выполняется на уровне роутера.

Видимость:
- **super_admin** — все ``inbound_sms``; опц. фильтр ``team_id`` (по текущей
  принадлежности номера) и точный ``to_number``.
- **group_member / group_leader** — только SMS номеров своих команд
  (``team_ids``, ADR-0012); ``team_id`` игнорируется; ``to_number`` вне scope →
  пустой результат (анти-энумерация, не 403/404).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cursor import decode_cursor, encode_cursor
from app.exceptions import InvalidLimitError
from app.infrastructure.repositories import PhoneNumberRepository, SmsRepository
from shared.models import InboundSms

DEFAULT_LIMIT = 50
MIN_LIMIT = 1
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class MessagePage:
    """Страница просмотра SMS: ORM-строки + следующий opaque-курсор."""

    rows: list[InboundSms]
    next_cursor: str | None


class MessageQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def _visible_numbers(
        self, *, is_super_admin: bool, team_ids: frozenset[int], team_id: int | None
    ) -> list[str] | None:
        """Набор видимых номеров (``to_number IN (...)``) по роли.

        ``None`` — без фильтра (super_admin без ``team_id`` → все SMS). Пустой
        список — видимых номеров нет (пустой результат).
        """
        repo = PhoneNumberRepository(self._s)
        if is_super_admin:
            if team_id is None:
                return None
            # Фильтр по текущей принадлежности номера команде (ADR-0014 §2).
            numbers = await repo.list_by_team(team_id)
            return [n.phone_number for n in numbers]
        # Участник/лидер: номера всех своих команд (team_ids, ADR-0012).
        # team_id игнорируется. Пустой scope → пустой результат.
        if not team_ids:
            return []
        numbers = await repo.list_by_teams(team_ids)
        return [n.phone_number for n in numbers]

    async def list_messages(
        self,
        *,
        is_super_admin: bool,
        team_ids: frozenset[int],
        to_number: str | None,
        team_id: int | None,
        cursor: str | None,
        limit: int,
    ) -> MessagePage:
        """Отдать страницу SMS по правилам видимости и keyset-пагинации.

        :raises InvalidLimitError: ``limit`` вне ``[1, 100]``.
        :raises InvalidCursorError: битый ``cursor`` (из :func:`decode_cursor`).
        """
        if limit < MIN_LIMIT or limit > MAX_LIMIT:
            raise InvalidLimitError(
                detail=f"limit вне диапазона [{MIN_LIMIT},{MAX_LIMIT}]"
            )
        decoded = decode_cursor(cursor) if cursor else None

        to_numbers = await self._visible_numbers(
            is_super_admin=is_super_admin, team_ids=team_ids, team_id=team_id
        )
        rows = await SmsRepository(self._s).list_inbound(
            to_numbers=to_numbers,
            to_number=to_number,
            cursor=decoded,
            limit=limit + 1,
        )

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(last.received_at, last.id)

        return MessagePage(rows=page, next_cursor=next_cursor)
