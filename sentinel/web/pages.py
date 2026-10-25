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
    "injection_ml": "Possible instructions in seller text", "injection_unavailable": "Could not check seller text",
    "bait_switch": "Bait and switch",
    "listing_changed": "Sells something other than listed", "flagged_wallet": "Wallet flagged as a scam",
    "unused_wallet": "Wallet has no history", "bad_deliveries": "Seller's deliveries keep failing",
    "escrow_terms": "Escrow terms not safe",
    "approval": "Seller not approved", "quote_changed": "Quote changed",
    "seller_escrow": "Not on your list, paid through escrow",
    "wallet_risk": "Seller not approved", "wallet_unknown": "Seller not approved",  # rows from the removed wallet model
    "apify_daily": "Over the shared Apify daily budget", "unlisted": "Not on your list, paid directly",
}
# The models behind a decision (payment_attempts.ml_model, migration 019): shown name and what crossing the line does.
ML_MODELS = {"injection": ("Injection model", "blocks")}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def why(payment: Mapping[str, Any]) -> tuple[str, str]:
    """The Payments page's Why cell as (label, detail): plain English, empty for a settled payment.

    Sentinel decides every payment itself, so nothing here waits for the owner. A payment Sentinel stopped (blocked,
    denied, or escalated by an older version) gets a friendly label for the rule and its own sentence as detail. A
    failed one says it did not complete (or, with a tx hash, that it was paid but not recorded); an approved one is in
    progress. An escrowed one is held while Sentinel checks the delivery; a voided one was refunded because the
    delivery failed the check; a settled one whose delivery failed the check (paid without escrow) says so. Rows from
    the old approval flow (pending, authorized, cancelled, expired) were never paid.
    """
    status = payment.get("status")
    if status in ("blocked", "denied", "escalated"):
        rule = (payment.get("rule") or "").strip()
        label = RULE_LABELS.get(rule) or rule.replace("_", " ").title() or "Blocked"
        return label, (payment.get("detail") or "").strip()
    if status == "failed":
        if payment.get("tx_hash"):
            return "Paid but not recorded, contact the owner", ""
        return "Payment did not complete", ""
    if status == "approved":
        return "In progress", ""
    _, _, delivery = (payment.get("delivery_reason") or "").partition(": ")
    if status == "escrowed":
        return "In escrow while Sentinel checks the delivery", ""
    if status == "voided":
        return "Refunded: the delivery failed the check", delivery
    if status == "settled" and payment.get("delivery_verdict") == "fail":
        return "Paid, but the delivery failed the check (no escrow)", delivery
    if status in ("pending", "authorized", "cancelled", "expired"):
        return "Not paid", "Nothing was signed."
    return "", ""


def ml_badge(payment: Mapping[str, Any]) -> dict[str, Any] | None:
    """The model score behind a payment as {model, text, hit}, or None when no model scored it.

    text reads like "Injection model 0.97 · blocks at 0.42"; hit is True when the score reached the threshold (the
    model stopped the payment), so the page shows it red, else neutral.
    """
    model, score, threshold = payment.get("ml_model"), payment.get("ml_score"), payment.get("ml_threshold")
    if not model or score is None:
        return None
    name, verb = ML_MODELS.get(model, (f"{str(model).replace('_', ' ').capitalize()} model", "stops"))
    text = f"{name} {float(score):.2f}"
    if threshold is not None:
        text += f" · {verb} at {float(threshold):.2f}"
    return {"model": model, "text": text, "hit": threshold is not None and float(score) >= float(threshold)}


# Rules the spending guard (and the seller's payment offer) fire; every other rule that stops a payment is intent's.
GUARD_RULES = frozenset({
    "seller", "cap", "budget", "price_jump", "duplicate", "url", "long_window", "lookalike", "payee_changed",
    "flagged_wallet", "payment_option", "escrow_terms", "quote", "quote_changed", "wallet_risk", "wallet_unknown",
})
# The guard rules that are Sentinel's seller decision: the owner's lists, then escrow only for the rest.
SELLER_RULES = frozenset({"seller", "wallet_risk", "wallet_unknown", "approval"})
LEGACY_STATUSES = ("pending", "authorized", "denied", "cancelled", "expired")  # the old approval flow's rows
TIER_TEXT = {0: "Rules passed", 1: "Classifier: on task", 2: "Judge: on task"}


def _step(key: str, label: str, state: str, note: str = "", tx: str | None = None,
          ml: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "state": state, "note": note, "tx": tx, "ml": ml}


def _with(note: str, badge: dict[str, Any] | None) -> str:
    return f"{note}. {badge['text']}" if badge else note


def pipeline(payment: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Live page's stepper for one attempt: Found, Policy, Seller, Task check, Payment, Delivery, Result.

    Each step is a dict of key, label, state (ok, stop, wait or idle: passed, stopped here, in progress, not reached),
    note (plain English), tx (a transaction hash to link, or None) and ml (pages.ml_badge of the model that decided
    this step, or None). Sentinel decides every step itself: wait only means it is still working (paying, or checking
    an escrowed delivery). Steps after the one that stopped are idle.
    """
    if payment.get("kind") == "apify":
        return apify_pipeline(payment)
    status = payment.get("status") or ""
    rule = (payment.get("rule") or "").strip()
    detail = (payment.get("detail") or "").strip()
    label = RULE_LABELS.get(rule) or rule.replace("_", " ").capitalize()
    stopped = status in ("blocked", "denied", "escalated")
    escrow = payment.get("escrow_state")
    tier = payment.get("intent_tier")
    badge = ml_badge(payment)
    steps = []

    if payment.get("found_while_searching"):
        steps.append(_step("found", "Found", "ok", "While searching the catalog; never shown to the agent"))
    else:
        found = (payment.get("found_at") or "").strip()  # written by the agent, so shown as its claim
        where = host_path(found) if "://" in found else found
        steps.append(_step("found", "Found", "ok", f"The agent says it found this via {where}" if where
                           else "The agent asked to pay"))

    seller_stop = (stopped and rule in SELLER_RULES) or status in LEGACY_STATUSES
    guard_stop = stopped and rule in GUARD_RULES and not seller_stop
    steps.append(_step("guard", "Policy", "stop" if guard_stop else "ok",
                       f"{label}. {detail}".strip() if guard_stop else "Cap, budget, price and wallet checks"))

    if guard_stop:
        steps.append(_step("seller", "Seller", "idle"))
    elif seller_stop:
        if status in LEGACY_STATUSES:
            note = "Not on your approved list, nothing was paid"
        else:
            note = f"{label}. {detail}".strip() if detail else label
        steps.append(_step("seller", "Seller", "stop", note))
    elif rule == "seller_escrow":
        steps.append(_step("seller", "Seller", "ok", "Not on your list, so paid through escrow"))
    elif payment.get("seller"):
        steps.append(_step("seller", "Seller", "ok", "On your approved list"))
    else:
        steps.append(_step("seller", "Seller", "ok", "Not on your list"))

    injection = badge if badge and badge["model"] == "injection" else None
    intent_stop = stopped and not guard_stop and not seller_stop
    if guard_stop or seller_stop:
        steps.append(_step("intent", "Task check", "idle"))
    elif intent_stop:
        steps.append(_step("intent", "Task check", "stop", _with(f"{label}. {detail}".strip(), injection),
                           ml=injection))
    else:
        note = TIER_TEXT.get(tier, "Passed") if tier is not None else "Passed"
        if payment.get("intent_reason") and tier:
            note = f"{note}. {payment['intent_reason']}"
        steps.append(_step("intent", "Task check", "ok", _with(note, injection), ml=injection))

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
    elif verdict == "fail" or (verdict == "unsure" and status != "escrowed"):
        steps.append(_step("delivery", "Delivery check", "stop", reason or "The data failed the check"))
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
        steps.append(_step("result", "Result", "wait", "Money held until the check passes, else refunded"))
    elif status == "approved":
        steps.append(_step("result", "Result", "wait", "In progress"))
    elif status == "failed":
        steps.append(_step("result", "Result", "stop", "Payment did not complete"))
    else:
        steps.append(_step("result", "Result", "stop", "Stopped by Sentinel, nothing paid"))
    return steps


APIFY_POLICY_RULES = frozenset({"cap", "budget", "apify_daily"})
APIFY_RUNNING = ("STARTING", "READY", "RUNNING", "TIMING-OUT", "ABORTING")


def apify_pipeline(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The same seven steps for an Apify credit run (an apify_runs row from books.list_apify_runs).

    Amounts are millionths of USD, the same scale as USDC atomic units. Credits are not refundable, so a failed
    delivery check stops the Result step but cannot give the money back.
    """
    status = run.get("status") or ""
    rule = (run.get("rule") or "").strip()
    detail = (run.get("detail") or "").strip()
    label = RULE_LABELS.get(rule) or rule.replace("_", " ").capitalize()
    blocked = status == "BLOCKED"
    policy_stop = blocked and rule in APIFY_POLICY_RULES
    badge = ml_badge(run)
    injection = badge if badge and badge["model"] == "injection" else None
    cap, spent = run.get("max_charge"), run.get("actual_charge")
    verdict = run.get("delivery_verdict")
    _, _, reason = (run.get("delivery_reason") or "").partition(": ")
    reason = reason or (run.get("delivery_reason") or "")
    steps = [_step("found", "Found", "ok", "From the Apify Store")]

    steps.append(_step("guard", "Policy", "stop" if policy_stop else "ok",
                       f"{label}. {detail}".strip() if policy_stop else "Cap, budget and daily credit checks"))
    steps.append(_step("seller", "Seller", "idle" if policy_stop else "ok",
                       "" if policy_stop else "Apify Store, paid with Apify credits, not your wallet"))
    if policy_stop:
        steps.append(_step("intent", "Task check", "idle"))
    elif blocked:
        steps.append(_step("intent", "Task check", "stop", _with(f"{label}. {detail}".strip(), injection),
                           ml=injection))
    else:
        tier = run.get("intent_tier")
        note = TIER_TEXT.get(tier, "Passed") if tier is not None else "Passed"
        if run.get("intent_reason") and tier:
            note = f"{note}. {run['intent_reason']}"
        steps.append(_step("intent", "Task check", "ok", _with(note, injection), ml=injection))

    if blocked:
        steps.append(_step("payment", "Payment", "idle"))
    elif status == "REJECTED":
        steps.append(_step("payment", "Payment", "stop", "Apify rejected the run, nothing was spent"))
    elif status == "UNKNOWN":
        steps.append(_step("payment", "Payment", "stop", f"Start uncertain, the {usd(cap)} cap stays reserved"))
    elif status in APIFY_RUNNING:
        steps.append(_step("payment", "Payment", "wait", f"Running on Apify, capped at {usd(cap)}"))
    elif status == "SUCCEEDED":
        steps.append(_step("payment", "Payment", "ok", f"Run on Apify credits, capped at {usd(cap)}"))
    elif status == "TIMED-OUT":
        steps.append(_step("payment", "Payment", "ok", "Reached the time limit, kept the partial results"))
    else:
        steps.append(_step("payment", "Payment", "stop", f"The run ended {status.lower()}"))

    if verdict == "pass":
        steps.append(_step("delivery", "Delivery check", "ok", reason or "The results match what was bought"))
    elif verdict == "fail":
        steps.append(_step("delivery", "Delivery check", "stop", reason or "The results failed the check"))
    elif status in ("SUCCEEDED", "TIMED-OUT"):
        steps.append(_step("delivery", "Delivery check", "wait", "Checking the results"))
    else:
        steps.append(_step("delivery", "Delivery check", "idle"))

    cost = usd(spent) if spent is not None else "the cap"
    if blocked:
        steps.append(_step("result", "Result", "stop", "Stopped by Sentinel, nothing spent"))
    elif status == "REJECTED":
        steps.append(_step("result", "Result", "stop", "Nothing spent"))
    elif status == "UNKNOWN":
        steps.append(_step("result", "Result", "stop", "Uncertain, check the Apify console"))
    elif status in APIFY_RUNNING:
        steps.append(_step("result", "Result", "wait", "Running"))
    elif status in ("SUCCEEDED", "TIMED-OUT") and verdict == "pass":
        partial = " (partial results)" if status == "TIMED-OUT" else ""
        steps.append(_step("result", "Result", "ok", f"Spent {cost} of the {usd(cap)} cap{partial}"))
    elif status in ("SUCCEEDED", "TIMED-OUT") and verdict == "fail":
        steps.append(_step("result", "Result", "stop", f"Spent {cost}, but the results failed the check"))
    elif status in ("SUCCEEDED", "TIMED-OUT"):
        steps.append(_step("result", "Result", "wait", f"Spent {cost}, checking the results"))
    else:
        steps.append(_step("result", "Result", "stop", f"Run failed, {cost} spent"))
    return steps


def money_out(payment: Mapping[str, Any]) -> dict[str, Any]:
    """A Live card's money lane: label, got and of (atomic units) and kind (paid, escrow, refunded, progress or
    blocked, the card's colour), for payments and Apify runs alike."""
    if payment.get("kind") == "apify":
        status, verdict = payment.get("status"), payment.get("delivery_verdict")
        if status in ("SUCCEEDED", "TIMED-OUT"):
            kind = "blocked" if verdict == "fail" else "paid" if verdict == "pass" else "progress"
        else:
            kind = "progress" if status in APIFY_RUNNING else "blocked"
        return {"label": "Apify charged", "got": payment.get("actual_charge") or 0, "of": payment.get("max_charge"),
                "kind": kind}
    status = payment.get("status")
    if status == "settled" and payment.get("delivery_verdict") == "fail":
        kind = "blocked"
    else:
        kind = {"settled": "paid", "escrowed": "escrow", "voided": "refunded", "approved": "progress"}.get(
            status or "", "blocked")
    got = payment.get("amount") if status in ("settled", "escrowed") else 0
    return {"label": "Seller got", "got": got, "of": payment.get("amount"), "kind": kind}


def outcome(payment: Mapping[str, Any]) -> str:
    """A Live card's tone from its last step: ok, stop or wait (still in progress)."""
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
                             pipeline=pipeline, outcome=outcome, iso=iso, ml_badge=ml_badge,
                             money_out=money_out)
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
