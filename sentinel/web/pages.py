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
    "no_task": "No task", "no_mandate": "Task ended", "mandate_budget": "Over the task's budget",
    "rate": "Too many payments", "loop": "Bought again and again", "split": "Possibly split purchase",
    "per_call": "Pricier than the task expects", "drift": "Drifting from the task", "off_task": "Not part of the task",
    "judge": "Judged against the task", "judge_unavailable": "Could not check the task",
    "big_share": "Large share of the task's budget", "long_window": "Payment window too long",
    "lookalike": "Lookalike address", "payee_changed": "Payee swapped",
    "injection": "Seller tried to instruct the agent", "unlisted_url": "URL not from the catalog",
    "bait_switch": "Bait and switch",
    "listing_changed": "Sells something other than listed", "flagged_wallet": "Wallet flagged as a scam",
    "unused_wallet": "Wallet has no history", "bad_deliveries": "Seller's deliveries keep failing",
    "escrow_terms": "Escrow terms not safe",
    "approval": "Purchase approval", "quote_changed": "Quote changed",
}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def why(payment: Mapping[str, Any]) -> tuple[str, str]:
    """The Payments page's Why cell as (label, detail): plain English, empty for a settled payment.

    A blocked or denied payment gets a friendly label for the guard's (or intent binding's) rule and its own sentence
    as detail. A held (escalated) one says it waits for the owner. A failed one says it did not complete (or, with a
    tx hash, that it was paid but not recorded); an approved or pending one is in progress. An escrowed one is held
    while Sentinel checks the delivery (or waits for the owner); a voided one was refunded because the delivery failed
    the check; a settled one whose delivery failed the check (paid without escrow) says so.
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
    if status == "approved":
        return "In progress", ""
    if status == "escalated":
        rule = (payment.get("rule") or "").strip()
        return f"Held for you: {RULE_LABELS.get(rule, 'needs your decision').lower()}", (payment.get("detail") or "")
    _, _, delivery = (payment.get("delivery_reason") or "").partition(": ")
    if status == "escrowed":
        if payment.get("delivery_verdict") == "unsure":
            return "In escrow, waiting for you on Tasks", delivery
        return "In escrow while Sentinel checks the delivery", ""
    if status == "voided":
        return "Refunded: the delivery failed the check", delivery
    if status == "settled" and payment.get("delivery_verdict") == "fail":
        return "Paid, but the delivery failed the check (no escrow)", delivery
    if status in ("pending", "authorized", "cancelled", "expired"):
        return {
            "pending": ("Waiting for your approval", "Nothing has been signed."),
            "authorized": ("Approved once", "Ask your agent to resume before approval expires."),
            "cancelled": ("Approval dismissed", "Nothing was signed."),
            "expired": ("Approval expired", "Nothing was signed."),
        }[status]
    return "", ""


# Rules the spending guard (and the seller's payment offer) fire; every other rule that stops a payment is intent's.
GUARD_RULES = frozenset({
    "seller", "cap", "budget", "price_jump", "duplicate", "url", "long_window", "lookalike", "payee_changed",
    "flagged_wallet", "payment_option", "escrow_terms", "quote", "quote_changed",
})
APPROVAL_STATUSES = ("pending", "authorized", "denied", "cancelled", "expired")
TIER_TEXT = {0: "Rules passed", 1: "Classifier: on task", 2: "Judge: on task"}


def _step(key: str, label: str, state: str, note: str = "", tx: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "state": state, "note": note, "tx": tx}


def pipeline(payment: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Live page's stepper for one attempt: Found, Policy, Task check, (Approval), Payment, Delivery, Result.

    Each step is a dict of key, label, state (ok, stop, wait or idle: passed, stopped here, waiting, not reached),
    note (plain English) and tx (a transaction hash to link, or None). Steps after the one that stopped are idle.
    """
    status = payment.get("status") or ""
    rule = (payment.get("rule") or "").strip()
    detail = (payment.get("detail") or "").strip()
    label = RULE_LABELS.get(rule) or rule.replace("_", " ").capitalize()
    stopped = status in ("blocked", "denied", "escalated")
    escrow = payment.get("escrow_state")
    tier = payment.get("intent_tier")
    steps = []

    if payment.get("found_while_searching"):
        steps.append(_step("found", "Found", "ok", "While searching the catalog; never shown to the agent"))
    else:
        found = (payment.get("found_at") or "").strip()  # written by the agent, so shown as its claim
        where = host_path(found) if "://" in found else found
        steps.append(_step("found", "Found", "ok", f"The agent says it found this via {where}" if where
                           else "The agent asked to pay"))

    guard_stop = stopped and rule in GUARD_RULES
    steps.append(_step("guard", "Policy", "stop" if guard_stop else "ok",
                       f"{label}. {detail}".strip() if guard_stop else "Seller, cap, budget, price and wallet checks"))

    intent_stop = stopped and not guard_stop and rule != "approval"
    if guard_stop:
        steps.append(_step("intent", "Task check", "idle"))
    elif intent_stop:
        note = f"{label}. {detail}".strip()
        steps.append(_step("intent", "Task check", "wait" if status == "escalated" else "stop",
                           f"Held for you: {note}" if status == "escalated" else note))
    else:
        note = TIER_TEXT.get(tier, "Passed") if tier is not None else "Passed"
        if payment.get("intent_reason") and tier:
            note = f"{note}. {payment['intent_reason']}"
        steps.append(_step("intent", "Task check", "ok", note))

    if status in APPROVAL_STATUSES or rule == "approval":
        state, note = {
            "pending": ("wait", "Waiting for your Yes"), "authorized": ("ok", "You approved it once"),
            "denied": ("stop", "You said no"), "cancelled": ("stop", "Approval dismissed"),
            "expired": ("stop", "Approval expired"),
        }.get(status, ("stop", detail or "Approval unavailable") if stopped else ("ok", "You approved this seller"))
        steps.append(_step("approval", "Your approval", state, note))

    reached = status in ("approved", "settled", "failed", "escrowed", "voided")
    if not reached:
        steps.append(_step("payment", "Payment", "idle"))
    elif escrow:
        steps.append(_step("payment", "Payment", "ok", "Locked in escrow, the seller is not paid yet",
                           payment.get("authorize_tx")))
    elif status == "approved":
        steps.append(_step("payment", "Payment", "wait", "Signing and settling"))
    elif status == "failed":
        steps.append(_step("payment", "Payment", "stop", "Did not complete", payment.get("tx_hash")))
    else:
        steps.append(_step("payment", "Payment", "ok", "Paid directly (no escrow offered)", payment.get("tx_hash")))

    verdict = payment.get("delivery_verdict")
    _, _, reason = (payment.get("delivery_reason") or "").partition(": ")
    reason = reason or (payment.get("delivery_reason") or "")
    if verdict == "pass":
        steps.append(_step("delivery", "Delivery check", "ok", reason or "The data matches what was bought"))
    elif verdict == "fail":
        steps.append(_step("delivery", "Delivery check", "stop", reason or "The data failed the check"))
    elif verdict == "unsure":
        steps.append(_step("delivery", "Delivery check", "wait", reason or "Unsure, waiting for you"))
    elif status == "escrowed":
        steps.append(_step("delivery", "Delivery check", "wait", "Checking what the seller sent"))
    else:
        steps.append(_step("delivery", "Delivery check", "idle"))

    if status == "settled" and escrow == "captured":
        steps.append(_step("result", "Result", "ok", "Seller paid from escrow", payment.get("capture_tx")))
    elif status == "settled":
        steps.append(_step("result", "Result", "stop" if verdict == "fail" else "ok",
                           "Paid, but the delivery failed" if verdict == "fail" else "Paid", payment.get("tx_hash")))
    elif status == "voided":
        steps.append(_step("result", "Result", "stop", "Refunded to you, the seller got nothing",
                           payment.get("void_tx")))
    elif status == "escrowed":
        steps.append(_step("result", "Result", "wait", "Waiting for you on Tasks" if payment.get("escrow_held")
                           else "Money held until the check passes"))
    elif status == "escalated":
        steps.append(_step("result", "Result", "wait", "Held for you, nothing paid"))
    elif status in ("pending", "authorized"):
        steps.append(_step("result", "Result", "wait", "Nothing signed yet"))
    elif status == "approved":
        steps.append(_step("result", "Result", "wait", "In progress"))
    elif status == "failed":
        steps.append(_step("result", "Result", "stop", "Payment did not complete"))
    else:
        steps.append(_step("result", "Result", "stop", "Blocked, nothing paid"))
    return steps


def outcome(payment: Mapping[str, Any]) -> str:
    """A Live card's tone from its last step: ok, stop or wait."""
    return pipeline(payment)[-1]["state"]


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


def iso(value: datetime | None) -> str:
    """A timestamp as ISO 8601 UTC, for the Live page's ticking clocks."""
    return "" if value is None else value.astimezone(timezone.utc).isoformat()


templates.env.filters.update(usd=usd, short=short, when=when, why=why, host_path=host_path, account_name=account_name,
                             pipeline=pipeline, outcome=outcome, iso=iso)
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
