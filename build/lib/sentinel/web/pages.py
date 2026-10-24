"""Rendering the site's HTML pages: the one Jinja2 environment for sentinel/web/templates, two helpers and the filters.

oauth.py renders its error page through render_error (an /authorize error that must not redirect), and web/app.py
renders every site page through render and render_error. Autoescape is always on, so anything a client or a user
supplied (an app name, an address, an error description, a payment reason) is shown as text, never as markup.
The filters usd, short and when format money (atomic units), addresses and timestamps.
"""
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jinja2
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from sentinel import results

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR), autoescape=True)
)


def usd(atomic: int | None) -> str:
    """Atomic units as "$0.50" for the account pages; an empty string for None."""
    return "" if atomic is None else f"${results.usdc_text(int(atomic))}"


def short(text: str | None, head: int = 6, tail: int = 4) -> str:
    """A long address or hash as 0x1234...abcd (the whole text when it is already short)."""
    if not text or len(text) <= head + tail + 3:
        return text or ""
    return f"{text[:head]}...{text[-tail:]}"


def when(value: datetime | None) -> str:
    """A timestamp as "2026-10-24 14:05 UTC"."""
    return "" if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


RULE_LABELS = {
    "seller": "Seller not approved", "cap": "Over your per payment cap", "budget": "Over your weekly budget",
    "price_jump": "Price jumped", "duplicate": "Duplicate payment", "url": "Unsafe URL",
    "payment_option": "No supported payment option",
}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def why(payment: Mapping[str, Any]) -> tuple[str, str]:
    """The Payments page's Why cell as (label, detail): plain English, empty for a settled payment.

    A blocked or denied payment gets a friendly label for the guard's rule and the guard's own sentence as detail.
    A failed one says it did not complete (or, with a tx hash, that it was paid but not recorded); an approved or
    pending one is in progress.
    """
    status = payment.get("status")
    if status in ("blocked", "denied"):
        rule = (payment.get("rule") or "").strip()
        label = RULE_LABELS.get(rule) or rule.replace("_", " ").title() or "Blocked"
        return label, (payment.get("detail") or "").strip()
    if status == "failed":
        if payment.get("tx_hash"):
            return "Paid but not recorded, contact the owner", ""
        return "Payment did not complete", ""
    if status in ("approved", "pending"):
        return "In progress", ""
    return "", ""


def host_path(url: str | None) -> str:
    """A URL as host and path ("data.example/report"), the whole text when it will not parse."""
    try:
        parts = urlsplit(url or "")
        shown = f"{parts.hostname or ''}{parts.path.rstrip('/')}"
    except ValueError:
        return url or ""
    return shown or (url or "")


def account_name(name: str | None) -> str:
    """An account name with any wallet address inside it shortened ("USDC Cash 0x457c...a7b1")."""
    return ADDRESS.sub(lambda m: short(m.group(0)), name or "")


templates.env.filters.update(usd=usd, short=short, when=when, why=why, host_path=host_path, account_name=account_name)
templates.env.globals.update(tx_url=results.EXPLORER)


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
