"""Integration: переработанный ``GET /admin`` — паритет с mail-agregator.

Контракт — docs/05 §7 «Паритет /admin (НОРМАТИВНО)» + подраздел «SSR-контекст»,
ADR-0015 (визуальный паритет), ADR-0012 (multi-team). Покрывает:

* литеральные имена SSR-контекста (§7) + отсутствие ``has_telegram_link`` в строке;
* группировку (бакет «без команды»/super_admin первым → команды по ``team_name`` →
  лидер первым → ``username``); один пользователь = одна строка при мультичленстве;
* поиск ``q`` (case-insensitive substring, ``total`` до пагинации, empty-state);
* пагинацию (границы, дефолты, сохранение ``q`` в ссылках);
* рендер (6 колонок в порядке, единая таблица, чипы команд, секция номеров,
  отсутствие Telegram-колонки/индикатора);
* НЕ-регресс §4: ``GET /api/admin/users`` по-прежнему отдаёт
  ``has_telegram_link``/``team_ids``/``is_leader``.
"""

from __future__ import annotations

import re

import pytest
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.api.routers import admin_ui
from shared.db import make_session
from tests.conftest import make_auth, seed_membership, seed_team, seed_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- helpers -----------------------------------------------------------------


async def _super_admin(name: str = "ap-root") -> int:
    async with make_session() as s:
        async with s.begin():
            u = await seed_user(s, username=name, role="super_admin", team_id=None)
        return u.id


async def _capture_context(client, monkeypatch, cookies, url: str = "/admin"):
    """Вызвать ``GET /admin`` с подменённым ``render`` и вернуть SSR-контекст.

    Подмена ловит ПОЛНЫЙ merged-контекст роутера (сервисный dict + ``csrf_token`` +
    ``is_super_admin``), не парся HTML. Сервис при этом выполняется целиком.
    """
    captured: dict = {}

    async def _fake_render(request, template, context):
        captured["template"] = template
        captured["context"] = context
        return HTMLResponse("<captured></captured>")

    monkeypatch.setattr(admin_ui, "render", _fake_render)
    resp = await client.get(url, cookies=cookies)
    return resp, captured.get("context", {})


def _thead_columns(body: str) -> list[str]:
    """Извлечь тексты ``<th>`` из первого ``<thead>`` таблицы пользователей."""
    thead = re.search(r"<thead>(.*?)</thead>", body, re.S)
    assert thead, "нет <thead> в таблице пользователей"
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S)
    ]


# --- SSR-контекст: литеральные имена §7 --------------------------------------


async def test_admin_context_has_literal_names(client, monkeypatch):
    admin = await _super_admin("ap-ctx")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-ctx-team")
            await seed_user(s, username="ap-ctx-lead", role="group_leader", team_id=tid)
            await seed_user(s, username="ap-ctx-mem", role="group_member", team_id=tid)
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                    "VALUES ('+441260000001', NULL, true)"
                )
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    resp, ctx = await _capture_context(client, monkeypatch, cookies)
    assert resp.status_code == 200

    # Top-level литеральные имена (§7 «SSR-контекст»).
    for key in (
        "user_groups",
        "teams",
        "q",
        "total",
        "page",
        "limit",
        "unassigned_numbers",
        "csrf_token",
        "is_super_admin",
    ):
        assert key in ctx, f"нет ключа {key} в SSR-контексте /admin"
    assert ctx["is_super_admin"] is True
    assert isinstance(ctx["csrf_token"], str) and ctx["csrf_token"]

    # Бакеты: {team_id, team_name, users}.
    assert ctx["user_groups"], "user_groups не должен быть пустым"
    for bucket in ctx["user_groups"]:
        assert set(bucket.keys()) == {"team_id", "team_name", "users"}

    # Строка пользователя: ровно литеральные поля §7 (без has_telegram_link).
    expected_user_keys = {
        "id",
        "username",
        "display_name",
        "role",
        "home_team",
        "memberships",
        "created_at",
        "last_login_at",
        "password_reset_required",
    }
    all_users = [u for b in ctx["user_groups"] for u in b["users"]]
    assert all_users
    for u in all_users:
        assert set(u.keys()) == expected_user_keys, u.keys()
        assert "has_telegram_link" not in u
        assert "team_id" not in u  # заменён на home_team/memberships
        assert "is_leader" not in u

    # unassigned_numbers — только с team_id IS NULL, литеральные поля.
    assert len(ctx["unassigned_numbers"]) == 1
    n = ctx["unassigned_numbers"][0]
    assert set(n.keys()) == {"id", "phone_number", "label", "is_active", "created_at"}
    assert n["phone_number"] == "+441260000001"

    # teams — {id, name, leader_user_id}.
    assert ctx["teams"]
    for t in ctx["teams"]:
        assert set(t.keys()) == {"id", "name", "leader_user_id"}


async def test_admin_user_row_has_no_telegram_field(client, monkeypatch):
    """Строка /admin не несёт has_telegram_link даже при живой привязке (§7)."""
    admin = await _super_admin("ap-notg")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-notg-team")
            lead = await seed_user(
                s, username="ap-notg-lead", role="group_leader", team_id=tid
            )
            # живая telegram-привязка — не должна протечь в /admin-контекст.
            from app.infrastructure.repositories import TelegramLinkRepository

            await TelegramLinkRepository(s).upsert(
                telegram_user_id=880001, user_id=lead.id
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    _, ctx = await _capture_context(client, monkeypatch, cookies)
    rows = [u for b in ctx["user_groups"] for u in b["users"]]
    assert all("has_telegram_link" not in u for u in rows)


# --- Группировка -------------------------------------------------------------


async def test_admin_grouping_order(client, monkeypatch):
    """Бакет «без команды» (super_admin) первым → команды по team_name →
    внутри команды лидер первым, затем участники по username."""
    admin = await _super_admin("ap-ord")
    async with make_session() as s:
        async with s.begin():
            # Названия команд в обратном алфавиту порядке создания.
            t_zeta = await seed_team(s, "ap-ord-zeta")
            t_alpha = await seed_team(s, "ap-ord-alpha")
            # В zeta: лидер с «поздним» логином + участник с «ранним» логином.
            await seed_user(
                s, username="ap-ord-z-lead", role="group_leader", team_id=t_zeta
            )
            await seed_user(
                s, username="ap-ord-a-mem", role="group_member", team_id=t_zeta
            )
            await seed_user(
                s, username="ap-ord-alead", role="group_leader", team_id=t_alpha
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    _, ctx = await _capture_context(client, monkeypatch, cookies)
    groups = ctx["user_groups"]
    # Бакет super_admin («без команды», team_id=None) — первый.
    assert groups[0]["team_id"] is None
    assert groups[0]["users"][0]["username"] == "ap-ord"  # только super_admin
    # Далее команды по team_name: alpha раньше zeta.
    team_names = [g["team_name"] for g in groups if g["team_id"] is not None]
    assert team_names == ["ap-ord-alpha", "ap-ord-zeta"]
    # Внутри zeta: лидер первым, несмотря на username 'z' > 'a'.
    zeta = next(g for g in groups if g["team_name"] == "ap-ord-zeta")
    assert [u["username"] for u in zeta["users"]] == ["ap-ord-z-lead", "ap-ord-a-mem"]


async def test_admin_one_row_per_user_multiteam(client, monkeypatch):
    """Мультичленство: пользователь = ОДНА строка в home-бакете; все команды —
    в memberships (домашняя первой); дублирования строк нет (ADR-0012 §38)."""
    admin = await _super_admin("ap-mt")
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "ap-mt-home")
            extra = await seed_team(s, "ap-mt-extra")
            await seed_user(
                s, username="ap-mt-exlead", role="group_leader", team_id=extra
            )
            mem = await seed_user(
                s, username="ap-mt-mem", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=extra)
    cookies, _ = await make_auth(admin, "super_admin", None)
    _, ctx = await _capture_context(client, monkeypatch, cookies)

    # Пользователь встречается РОВНО один раз во всём наборе.
    occurrences = [
        (g["team_id"], u)
        for g in ctx["user_groups"]
        for u in g["users"]
        if u["id"] == mem.id
    ]
    assert len(occurrences) == 1
    bucket_tid, row = occurrences[0]
    assert bucket_tid == home  # в бакете домашней команды
    assert row["home_team"] == {"id": home, "name": "ap-mt-home"}
    # memberships = все команды, домашняя первой.
    mem_ids = [m["id"] for m in row["memberships"]]
    assert mem_ids[0] == home
    assert set(mem_ids) == {home, extra}


# --- Поиск q -----------------------------------------------------------------


async def test_admin_search_narrows_case_insensitive_and_total(client, monkeypatch):
    """q — case-insensitive substring по логину; total = число совпавших ДО
    пагинации; выдача сужается."""
    admin = await _super_admin("ap-srch-root")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-srch-team")
            await seed_user(s, username="alice", role="group_leader", team_id=tid)
            await seed_user(s, username="alicia", role="group_member", team_id=tid)
            await seed_user(s, username="bob", role="group_member", team_id=tid)
    cookies, _ = await make_auth(admin, "super_admin", None)
    # Верхний регистр запроса → проверка case-insensitivity.
    _, ctx = await _capture_context(client, monkeypatch, cookies, url="/admin?q=ALI")
    assert ctx["q"] == "ALI"
    names = {u["username"] for g in ctx["user_groups"] for u in g["users"]}
    assert names == {"alice", "alicia"}
    assert ctx["total"] == 2  # bob и super_admin не совпали


async def test_admin_search_empty_state_renders(client):
    """Пустой результат поиска → user_groups=[] и рендер empty-state (§7)."""
    admin = await _super_admin("ap-empty")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-empty-team")
            await seed_user(s, username="ap-empty-u", role="group_leader", team_id=tid)
    cookies, _ = await make_auth(admin, "super_admin", None)
    r = await client.get("/admin?q=zzz-no-such-login", cookies=cookies)
    assert r.status_code == 200
    body = r.text
    assert 'class="admin__empty"' in body
    assert 'role="status"' in body
    assert "не найдены" in body
    # Таблица пользователей не рендерится при пустом наборе.
    assert '<table class="admin-users-table">' not in body


# --- Пагинация ---------------------------------------------------------------


async def test_admin_pagination_slices_keep_total(client, monkeypatch):
    admin = await _super_admin("ap-pg-root")  # 1 запись
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-pg-team")
            for i in range(4):
                await seed_user(
                    s,
                    username=f"ap-pg-u{i}",
                    role="group_member" if i else "group_leader",
                    team_id=tid,
                )
    cookies, _ = await make_auth(admin, "super_admin", None)
    total_expected = 5  # super_admin + 4 участника
    # Страница 1, limit 2 → 2 записи, total=5.
    _, ctx1 = await _capture_context(
        client, monkeypatch, cookies, url="/admin?limit=2&page=1"
    )
    assert ctx1["total"] == total_expected
    assert ctx1["page"] == 1 and ctx1["limit"] == 2
    assert sum(len(g["users"]) for g in ctx1["user_groups"]) == 2
    # Последняя страница (3) → 1 запись.
    _, ctx3 = await _capture_context(
        client, monkeypatch, cookies, url="/admin?limit=2&page=3"
    )
    assert ctx3["total"] == total_expected
    assert sum(len(g["users"]) for g in ctx3["user_groups"]) == 1


async def test_admin_pagination_defaults(client, monkeypatch):
    admin = await _super_admin("ap-def")
    cookies, _ = await make_auth(admin, "super_admin", None)
    _, ctx = await _capture_context(client, monkeypatch, cookies)
    assert ctx["q"] == ""
    assert ctx["page"] == 1
    assert ctx["limit"] == 50


async def test_admin_pagination_bounds(client):
    admin = await _super_admin("ap-bnd")
    cookies, _ = await make_auth(admin, "super_admin", None)
    # Вне границ → отклонение (app маппит RequestValidationError → 400).
    # page ge=1.
    assert (await client.get("/admin?page=0", cookies=cookies)).status_code == 400
    # limit [1, 200].
    assert (await client.get("/admin?limit=0", cookies=cookies)).status_code == 400
    assert (await client.get("/admin?limit=201", cookies=cookies)).status_code == 400
    # Границы диапазона валидны.
    assert (await client.get("/admin?limit=200", cookies=cookies)).status_code == 200
    assert (
        await client.get("/admin?page=1&limit=1", cookies=cookies)
    ).status_code == 200


async def test_admin_pagination_preserves_q_in_links(client):
    admin = await _super_admin("ap-qlink")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-qlink-team")
            for i in range(4):
                await seed_user(
                    s,
                    username=f"ap-mem{i}",
                    role="group_member" if i else "group_leader",
                    team_id=tid,
                )
    cookies, _ = await make_auth(admin, "super_admin", None)
    # q='ap-mem' совпадает с 4 пользователями; limit=2 → 2 страницы.
    r = await client.get("/admin?q=ap-mem&limit=2&page=1", cookies=cookies)
    assert r.status_code == 200
    body = r.text
    assert 'rel="next"' in body  # есть следующая страница
    # Ссылка пагинации сохраняет q.
    assert "q=ap-mem" in body
    assert "page=2" in body

    # Follow-through: страница 2 достижима и отдаёт 200 с оставшейся записью.
    r2 = await client.get("/admin?q=ap-mem&limit=2&page=2", cookies=cookies)
    assert r2.status_code == 200
    assert 'rel="prev"' in r2.text


# --- Рендер: колонки, таблица, чипы, номера, отсутствие Telegram --------------


async def test_admin_render_six_columns_in_order(client):
    admin = await _super_admin("ap-cols")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-cols-team")
            await seed_user(s, username="ap-cols-u", role="group_leader", team_id=tid)
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    cols = _thead_columns(body)
    assert cols == ["Имя", "Роль", "Команда", "Создан", "Последний вход", "Действия"]
    # Единая таблица.
    assert body.count('<table class="admin-users-table">') == 1


async def test_admin_render_no_telegram_column_or_indicator(client):
    """Колонки/индикатора Telegram на /admin нет (Q-ADMIN-1 «a», §7)."""
    admin = await _super_admin("ap-tg")
    async with make_session() as s:
        async with s.begin():
            tid = await seed_team(s, "ap-tg-team")
            lead = await seed_user(
                s, username="ap-tg-lead", role="group_leader", team_id=tid
            )
            from app.infrastructure.repositories import TelegramLinkRepository

            await TelegramLinkRepository(s).upsert(
                telegram_user_id=990001, user_id=lead.id
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    # Нет колонки Telegram среди заголовков.
    assert "Telegram" not in _thead_columns(body)
    # Имя поля не протекает в разметку.
    assert "has_telegram_link" not in body
    # Внутри таблицы пользователей нет Telegram-упоминаний/статус-индикатора
    # привязки (SDK-скрипт telegram-web-app.js и текст диалога удаления — вне
    # таблицы, поэтому проверку скоупим на регион таблицы).
    table = re.search(r'<table class="admin-users-table">(.*?)</table>', body, re.S)
    assert table, "таблица пользователей не найдена"
    table_html = table.group(1).lower()
    assert "telegram" not in table_html
    assert "привяз" not in table_html


async def test_admin_render_team_chips_home_and_extra(client):
    """Домашний чип без «×»; доп.-чип с формой удаления (_method=DELETE)."""
    admin = await _super_admin("ap-chip")
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "ap-chip-home")
            extra = await seed_team(s, "ap-chip-extra")
            await seed_user(
                s, username="ap-chip-exlead", role="group_leader", team_id=extra
            )
            mem = await seed_user(
                s, username="ap-chip-mem", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=extra)
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    assert "team-chip--home" in body
    # Доп.-чип несёт форму удаления на same-path + _method=DELETE.
    assert f'action="/api/admin/users/{mem.id}/teams/{extra}"' in body
    assert 'name="_method" value="DELETE"' in body
    # Домашний чип формы удаления НЕ имеет.
    assert f'action="/api/admin/users/{mem.id}/teams/{home}"' not in body


async def test_admin_no_unassigned_numbers_section(client):
    """Секция «Нераспределённые номера» убрана с /admin — управление всеми
    номерами перенесено на страницу /numbers."""
    admin = await _super_admin("ap-num")
    async with make_session() as s:
        async with s.begin():
            await seed_team(s, "ap-num-team")
            await s.execute(
                text(
                    "INSERT INTO phone_numbers (phone_number, team_id, is_active) "
                    "VALUES ('+441260000099', NULL, true)"
                )
            )
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    assert "Нераспределённые номера" not in body
    assert "+441260000099" not in body
    assert "data-numbers-section" not in body


async def test_admin_render_rename_control(client):
    """Меню «+» содержит пункт «Сменить имя» и диалог переименования (для не-super)."""
    admin = await _super_admin("ap-rename")
    async with make_session() as s:
        async with s.begin():
            t = await seed_team(s, "ap-rename-team")
            await seed_user(s, username="ap-rename-m", role="group_member", team_id=t)
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    assert "data-admin-actions-rename" in body  # пункт меню «Сменить имя»
    assert "data-admin-rename-dialog" in body  # диалог переименования
    assert "data-display-name=" in body  # префилл текущего имени на кнопке «+»


async def test_admin_render_tbody_banding_and_spacer(client):
    """Единая таблица: командные бакеты — <tbody> с band-a/-b, super_admin —
    --no-group, между бакетами — __spacer (docs/06 §40)."""
    admin = await _super_admin("ap-band")
    async with make_session() as s:
        async with s.begin():
            t1 = await seed_team(s, "ap-band-1")
            t2 = await seed_team(s, "ap-band-2")
            await seed_user(s, username="ap-band-l1", role="group_leader", team_id=t1)
            await seed_user(s, username="ap-band-l2", role="group_leader", team_id=t2)
    cookies, _ = await make_auth(admin, "super_admin", None)
    body = (await client.get("/admin", cookies=cookies)).text
    assert "user-group--band-a" in body
    assert "user-group--band-b" in body  # чередование по командным бакетам
    assert "user-group--no-group" in body  # бакет super_admin
    assert "user-group__spacer" in body  # зазор между бакетами
    # Старые классы банда/секции «Администраторы» не должны вернуться. (Класс
    # admin__group-head легитимно используется секцией «Нераспределённые номера»,
    # поэтому проверяем именно старые banding-/team-классы списка пользователей.)
    assert "admin__group--band-a" not in body
    assert "admin__group--no-team" not in body


# --- НЕ-регресс §4: JSON GET /api/admin/users --------------------------------


async def test_api_admin_users_keeps_telegram_and_team_fields(client):
    """JSON §4 по-прежнему содержит has_telegram_link/team_ids/is_leader,
    даже когда /admin их не показывает (раздельные контракты §4 vs §7)."""
    admin = await _super_admin("ap-json")
    async with make_session() as s:
        async with s.begin():
            home = await seed_team(s, "ap-json-home")
            extra = await seed_team(s, "ap-json-extra")
            await seed_user(
                s, username="ap-json-exlead", role="group_leader", team_id=extra
            )
            mem = await seed_user(
                s, username="ap-json-mem", role="group_member", team_id=home
            )
            await seed_membership(s, user_id=mem.id, team_id=extra)
    cookies, headers = await make_auth(admin, "super_admin", None)
    r = await client.get("/api/admin/users", cookies=cookies, headers=headers)
    assert r.status_code == 200
    users = r.json()["users"]
    entry = next(u for u in users if u["username"] == "ap-json-mem")
    assert "has_telegram_link" in entry
    assert "team_ids" in entry
    assert "is_leader" in entry
    assert sorted(entry["team_ids"]) == sorted([home, extra])
    assert entry["is_leader"] is False
