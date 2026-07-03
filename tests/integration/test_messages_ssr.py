"""Integration: SSR GET /messages — рендер, no-JS fallback (docs/06 §51).

Все роли → 200 (рендер, не редирект); аноним → 302 /login; ссылка «Ещё»
(next) рендерится только при next_cursor != null и её цель отдаёт 200
(follow-through, а не только 30x/значение cursor). Битый cursor в query → не 500.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.application.messages_service import DEFAULT_LIMIT
from app.infrastructure.repositories import SmsRepository
from shared.db import make_session
from tests.conftest import make_auth, seed_number, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_TS = datetime(2026, 7, 3, 9, 0, 0, tzinfo=UTC)


def _hidden_limit(html: str) -> int:
    """Значение скрытого поля формы ``limit`` = контекстная переменная ``limit``.

    Шаблон рендерит ``<input type="hidden" name="limit" value="{{ page_limit }}">``,
    где ``page_limit`` = контекст ``limit`` (docs/05 §9). Это наблюдаемый прокси
    контекстного ``limit`` (литеральное имя, НЕ ``selected_*``).
    """
    m = re.search(r'name="limit"\s+value="(\d+)"', html)
    assert m is not None, "скрытое поле limit не найдено в форме фильтра"
    return int(m.group(1))


async def _seed_sms(s, *, to_number, team_id, received_at, sid=None):
    return await SmsRepository(s).create(
        twilio_message_sid=sid,
        from_number="+12025550000",
        to_number=to_number,
        body="hello-body",
        team_id=team_id,
        raw_payload={"MessageSid": sid or "x"},
        received_at=received_at,
    )


async def _set_auth(client, user_id, role, team_id):
    cookies, _ = await make_auth(user_id, role, team_id)
    client.cookies.set("sms_session", cookies["sms_session"])


# --- Аноним → 302 /login -----------------------------------------------------


async def test_ssr_anonymous_redirects_login(client):
    r = await client.get("/messages")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- Каждая роль → 200 -------------------------------------------------------


async def test_ssr_super_admin_renders_200(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-root", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "ssr-Tsa")
            await seed_number(s, phone="+441000200001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441000200001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="SA-1",
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages")
    assert r.status_code == 200
    assert "Входящие SMS" in r.text
    # super_admin: селектор команды присутствует.
    assert 'name="team_id"' in r.text
    assert "+441000200001" in r.text


@pytest.mark.parametrize("role", ["group_leader", "group_member"])
async def test_ssr_team_role_renders_200(client, role):
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "ssr-T-" + role)
            lead = await seed_user(
                s, username=role + "-ssrl", role="group_leader", team_id=t1
            )
            if role == "group_member":
                u = await seed_user(
                    s, username=role + "-ssrm", role="group_member", team_id=t1
                )
            else:
                u = lead
            await seed_number(s, phone="+441000210001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441000210001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="TR-1",
            )
            u_id, t1_id = u.id, t1
    await _set_auth(client, u_id, role, t1_id)
    r = await client.get("/messages")
    assert r.status_code == 200
    assert "Входящие SMS" in r.text
    # Участник: селектора команды нет (team_id-фильтр не применяется).
    assert 'name="team_id"' not in r.text
    # Свой номер присутствует в селекторе/списке.
    assert "+441000210001" in r.text


# --- Ссылка «Ещё»: follow-through до 200; отсутствует при next_cursor=null ----


async def test_ssr_next_link_present_and_target_renders_200(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-more", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "ssr-Tmore")
            await seed_number(s, phone="+441000220001", team_id=t1)
            for i in range(4):
                await _seed_sms(
                    s,
                    to_number="+441000220001",
                    team_id=t1,
                    received_at=_BASE_TS + timedelta(seconds=i),
                    sid=f"MORE-{i}",
                )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"limit": 2})
    assert r.status_code == 200
    # Ссылка «Ещё» присутствует (next_cursor != null) с курсором в href.
    assert "data-messages-more" in r.text
    assert 'rel="next"' in r.text
    assert "cursor=" in r.text

    # Извлечь href ссылки «Ещё» и пройти по ней до терминального 200.
    import re

    m = re.search(r'href="([^"]*cursor=[^"]+)"[^>]*data-messages-more', r.text)
    if m is None:
        m = re.search(r'data-messages-more[^>]*href="([^"]*cursor=[^"]+)"', r.text)
    assert m is not None, "ссылка «Ещё» с cursor не найдена в разметке"
    href = m.group(1).replace("&amp;", "&")
    r2 = await client.get(href)
    # Цель редиректа/ссылки реально отдаёт 200 (не только наличие cursor).
    assert r2.status_code == 200
    assert "Входящие SMS" in r2.text


async def test_ssr_no_next_link_when_last_page(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-nomore", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "ssr-Tnomore")
            await seed_number(s, phone="+441000230001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441000230001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="NOMORE-1",
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"limit": 50})
    assert r.status_code == 200
    # next_cursor=null → ссылки «Ещё» нет.
    assert "data-messages-more" not in r.text


# --- Битый cursor в query → не 500 (страница деградирует) --------------------


async def test_ssr_broken_cursor_does_not_500(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-badcur", role="super_admin", team_id=None
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"cursor": "@@@garbage@@@"})
    # SSR деградирует (пустой список / 200), не роняется 500.
    assert r.status_code == 200
    assert "Входящие SMS" in r.text


# --- §4 SSR-контекст: ключи to_number/team_id/limit (НЕ selected_*) -----------
# docs/05 §9: имена контекстных переменных литеральные (to_number/team_id/limit).
# Наблюдаемые прокси: preselect <select name="to_number"/"team_id"> и hidden
# <input name="limit">. Если бы backend слал selected_*, preselect бы сломался,
# а limit-поле подставляло дефолт — эти тесты бы упали.


async def test_ssr_context_preselects_to_number_and_team(client):
    """Активные фильтры to_number/team_id отражаются в preselect (доказывает,
    что backend передаёт литеральные ключи to_number/team_id, а не selected_*)."""
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-preselect", role="super_admin", team_id=None
            )
            t1 = await seed_team(s, "ssr-Tpre")
            await seed_number(s, phone="+441000240001", team_id=t1)
            await _seed_sms(
                s,
                to_number="+441000240001",
                team_id=t1,
                received_at=_BASE_TS,
                sid="PRE-1",
            )
            admin_id, t1_id = admin.id, t1
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get(
        "/messages", params={"to_number": "+441000240001", "team_id": t1_id}
    )
    assert r.status_code == 200
    # <option value="+441000240001" ... selected> — фильтр номера preselect'нут.
    assert re.search(
        r'<option value="\+441000240001"[^>]*\bselected\b', r.text
    ), "to_number не preselect'нут (потерян ключ to_number)"
    # <option value="{t1_id}" ... selected> — фильтр команды preselect'нут.
    assert re.search(
        rf'<option value="{t1_id}"[^>]*\bselected\b', r.text
    ), "team_id не preselect'нут (потерян ключ team_id)"


async def test_ssr_valid_limit_reflected_in_context(client):
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-vlim", role="super_admin", team_id=None
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"limit": 10})
    assert r.status_code == 200
    # Валидный limit → фактический limit в контексте (hidden-поле формы).
    assert _hidden_limit(r.text) == 10


@pytest.mark.parametrize("bad", [0, 101, -5, 1000])
async def test_ssr_invalid_limit_falls_back_to_default(client, bad):
    """Невалидный limit на SSR → в контекст уходит DEFAULT_LIMIT (не битое
    значение), чтобы hidden-поле и следующий submit были валидны (ADR-0014)."""
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username=f"ssr-ilim{bad}", role="super_admin", team_id=None
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"limit": bad})
    # SSR не роняет 400 — деградирует к DEFAULT_LIMIT.
    assert r.status_code == 200
    assert _hidden_limit(r.text) == DEFAULT_LIMIT == 50


async def test_ssr_valid_limit_with_broken_cursor_keeps_limit(client):
    """Валидный limit + битый cursor → limit НЕ подменяется дефолтом (битым был
    только cursor; render_limit остаётся фактическим)."""
    async with make_session() as s:
        async with s.begin():
            admin = await seed_user(
                s, username="ssr-lc", role="super_admin", team_id=None
            )
            admin_id = admin.id
    await _set_auth(client, admin_id, "super_admin", None)
    r = await client.get("/messages", params={"limit": 7, "cursor": "@@@garbage@@@"})
    assert r.status_code == 200
    # cursor игнорируется, но валидный limit сохраняется (не 50).
    assert _hidden_limit(r.text) == 7


# --- §4 «Сообщения» в шапке — для всех аутентифицированных ролей --------------


@pytest.mark.parametrize(
    "role,team_named",
    [("super_admin", False), ("group_leader", True), ("group_member", True)],
)
async def test_ssr_messages_nav_link_for_all_roles(client, role, team_named):
    """Пункт «Сообщения» (href=/messages) в топ-навигации base.html доступен
    каждой аутентифицированной роли (docs/05 §7/§9)."""
    async with make_session() as s:
        async with s.begin():
            if team_named:
                t1 = await seed_team(s, "nav-T-" + role)
                lead = await seed_user(
                    s, username=role + "-navl", role="group_leader", team_id=t1
                )
                if role == "group_member":
                    u = await seed_user(
                        s, username=role + "-navm", role="group_member", team_id=t1
                    )
                else:
                    u = lead
                u_id, u_team = u.id, t1
            else:
                admin = await seed_user(
                    s, username="nav-root", role="super_admin", team_id=None
                )
                u_id, u_team = admin.id, None
    await _set_auth(client, u_id, role, u_team)
    r = await client.get("/messages")
    assert r.status_code == 200
    assert 'href="/messages"' in r.text
    assert "Сообщения" in r.text
