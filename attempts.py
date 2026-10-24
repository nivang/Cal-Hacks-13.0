"""Payment attempt log and the history lookups the guard needs.

Every payment request is written here, including blocked and paused ones, so the ledger shows what
did not happen as well as what did. Sits between payments.py (writes attempts) and guard.py
(reads the facts load_history returns).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from db import agent_id, insert, now

LIVE_STATUSES = ("settled", "approved", "pending")  # attempts that count as spent or in flight


def record_attempt(
    conn: sqlite3.Connection,
    agent: str,
    url: str,
    reason: str | None = None,
    quote: dict[str, Any] | None = None,
    status: str = "pending",
    decision: str | None = None,
    rule_fired: str | None = None,
) -> int:
    """Insert one payment attempt row and return its id.

    A row with status approved or pending takes the in flight lock for its dedupe key, so a second
    identical insert raises sqlite3.IntegrityError. Blocked rows never take the lock.
    """
    dedupe_key = quote and f"{agent}|{quote['pay_to'].lower()}|{url}|{quote['amount']}"
    return insert(
        conn,
        "INSERT INTO payment_attempts"
        " (agent_id, url, reason, pay_to, amount, quote_json, dedupe_key, decision, rule_fired, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id(conn, agent), url, reason,
            quote and quote["pay_to"], quote and quote["amount"], quote and json.dumps(quote),
            dedupe_key, decision, rule_fired, status, now(),
        ),
    )


def set_attempt_status(conn: sqlite3.Connection, attempt_id: int, status: str) -> None:
    """Move an attempt to a new status, e.g. failed. Releases the in flight lock when it leaves approved."""
    conn.execute("UPDATE payment_attempts SET status = ? WHERE id = ?", (status, attempt_id))


def load_agent(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Return the agent's name, per payment cap and weekly budget as a dict that builds guard.Agent."""
    row = conn.execute(
        "SELECT name, per_payment_cap, weekly_budget FROM agents WHERE name = ?", (name,)
    ).fetchone()
    return dict(row)


def load_history(conn: sqlite3.Connection, agent: str, quote: dict[str, Any], policy: dict[str, int]) -> dict[str, Any]:
    """Read the ledger facts guard.evaluate needs for this agent and quote.

    Returns seller_status, week_spend, seller_settled and duplicate as a dict that builds
    guard.History. Spend counts settled and in flight attempts so pending money is not double spent.
    """
    aid = agent_id(conn, agent)
    now_utc = datetime.now(timezone.utc)
    budget_start = (now_utc - timedelta(days=policy["budget_window_days"])).isoformat(timespec="seconds")
    duplicate_start = (now_utc - timedelta(seconds=policy["duplicate_window_s"])).isoformat(timespec="seconds")
    live = ",".join(f"'{s}'" for s in LIVE_STATUSES)

    seller = conn.execute(
        "SELECT status FROM sellers WHERE lower(pay_to) = lower(?) AND network = ? AND lower(asset) = lower(?)",
        (quote["pay_to"], quote["network"], quote["asset"]),
    ).fetchone()
    week_spend = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM payment_attempts WHERE agent_id = ? AND created_at >= ?"
        f" AND status IN ({live})",
        (aid, budget_start),
    ).fetchone()[0]
    settled = conn.execute(
        "SELECT amount FROM payment_attempts WHERE status = 'settled' AND lower(pay_to) = lower(?)"
        " ORDER BY settled_at DESC, id DESC LIMIT ?",
        (quote["pay_to"], policy["price_jump_window"]),
    ).fetchall()
    duplicate = conn.execute(
        f"SELECT 1 FROM payment_attempts WHERE agent_id = ? AND lower(pay_to) = lower(?) AND url = ? AND amount = ?"
        f" AND created_at >= ? AND status IN ({live}) LIMIT 1",
        (aid, quote["pay_to"], quote["url"], quote["amount"], duplicate_start),
    ).fetchone()
    return {
        "seller_status": seller["status"] if seller else None,
        "week_spend": week_spend,
        "seller_settled": tuple(r["amount"] for r in settled),
        "duplicate": duplicate is not None,
    }
