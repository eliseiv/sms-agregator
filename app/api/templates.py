"""Jinja2Templates singleton + render-хелпер.

Каталог ``templates/`` наполняет frontend-агент — backend предоставляет
рендерер и хелперы (``csrf_input``). ``render`` инжектит пустой ``flashes``
(frontend может расширить).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import Response

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_STATIC_VERSION = str(int(time.time()))


def _csrf_input(csrf_token: str) -> Markup:
    return Markup(
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
    )


templates.env.globals["csrf_input"] = _csrf_input
templates.env.globals["static_v"] = _STATIC_VERSION
templates.env.autoescape = True


async def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    """Отрендерить HTML-шаблон. ``flashes`` по умолчанию пуст."""
    base: dict[str, Any] = dict(context or {})
    base.setdefault("flashes", [])
    return templates.TemplateResponse(request, name, base, status_code=status_code)
