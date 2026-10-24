"""Rendering the site's HTML pages: the one Jinja2 environment for sentinel/web/templates and two small helpers.

oauth.py renders its error page through render_error (an /authorize error that must not redirect), and web/app.py
renders every site page through render and render_error. Autoescape is always on, so anything a client or a user
supplied (an app name, an address, an error description) is shown as text, never as markup.
"""
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jinja2
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR), autoescape=True)
)


def render(
    request: Request,
    name: str,
    context: Mapping[str, Any] | None = None,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """Render one template into an HTML response with this status and these extra headers."""
    return templates.TemplateResponse(
        request, name, dict(context or {}), status_code=status, headers=dict(headers or {})
    )


def render_error(request: Request, title: str, message: str, status: int) -> HTMLResponse:
    """Render the error page: a short title and one plain English sentence on what to do next."""
    return render(request, "error.html", {"title": title, "message": message}, status=status)
