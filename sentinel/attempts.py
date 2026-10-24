"""Payment attempt log and the history lookups the guard needs.

Every payment request is written here, including blocked ones, so the ledger shows what
did not happen as well as what did. Sits between payments.py (writes attempts) and guard.py
(reads the facts load_history returns). Amounts go in and out as integer atomic units.
"""
import json
from typing import Any

from sentinel.db import Connection, agent_id, insert, usdc

LIVE_STATUSES = ["settled", "approved"]  # attempts that count as spent or in flight
ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units


def record_attempt(
    conn: Connection,
    agent: str,
    url: str,
    reason: str | None = None,
    quote: dict[str, Any] | None = None,
    status: str = "blocked",  # fail closed: only an explicit "approved" takes the lock and can settle
    decision: str | None = None,
    rule_fired: str | None = None,
) -> int:
    """Insert one payment attempt row and return its id.

    A row with status approved takes the in flight lock for its dedupe key, so a second identical insert
    raises db.UniqueViolation. Blocked rows never take the lock.
    """
    dedupe_key = quote and f"{agent}|{quote['pay_to'].lower()}|{url}|{quote['amount']}"
    return insert(
        conn,
        "INSERT INTO payment_attempts"
        " (agent_id, url, reason, pay_to, amount, quote_json, dedupe_key, decision, rule_fired, status)"
        " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)",
        (
            agent_id(conn, agent), url, reason,
            quote and quote["pay_to"], quote and usdc(quote["amount"]), quote and json.dumps(quote),
            dedupe_key, decision, rule_fired, status,
        ),
    )


def set_attempt_status(conn: Connection, attempt_id: int, status: str) -> None:
    """Move an attempt to a new status, e.g. failed. Releases the in flight lock when it leaves approved."""
    conn.execute("UPDATE payment_attempts SET status = %s WHERE id = %s", (status, attempt_id))


def load_agent(conn: Connection, name: str) -> dict[str, Any]:
    """Return the agent's name, per payment cap and weekly budget (atomic units) as a dict that builds guard.Agent."""
    row = conn.execute(
        f"SELECT name, (per_payment_cap {ATOMIC})::bigint AS per_payment_cap,"
        f" (weekly_budget {ATOMIC})::bigint AS weekly_budget FROM agents WHERE name = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no agent named {name}")
    return row


def load_history(conn: Connection, agent: str, quote: dict[str, Any], policy: dict[str, int]) -> dict[str, Any]:
    """Read the ledger facts guard.evaluate needs for this agent and quote.

    Returns seller_status, week_spend, seller_settled, duplicate and duplicate_of as a dict that builds
    guard.History. Spend counts settled and in flight attempts so money on its way out is not double spent.
    Time windows are computed by the database clock.
    """
    aid = agent_id(conn, agent)
    seller = conn.execute(
        "SELECT status FROM sellers WHERE lower(pay_to) = lower(%s) AND network = %s AND lower(asset) = lower(%s)",
        (quote["pay_to"], quote["network"], quote["asset"]),
    ).fetchone()
    settled = conn.execute(
        f"SELECT (amount {ATOMIC})::bigint AS amount FROM payment_attempts"
        " WHERE status = 'settled' AND lower(pay_to) = lower(%s) ORDER BY settled_at DESC, id DESC LIMIT %s",
        (quote["pay_to"], policy["price_jump_window"]),
    ).fetchall()
    duplicate = conn.execute(
        "SELECT id FROM payment_attempts WHERE agent_id = %s AND lower(pay_to) = lower(%s) AND url = %s"
        " AND amount = %s AND created_at >= now() - make_interval(secs => %s) AND status = ANY(%s)"
        " ORDER BY id DESC LIMIT 1",
        (aid, quote["pay_to"], quote["url"], usdc(quote["amount"]), policy["duplicate_window_s"], LIVE_STATUSES),
    ).fetchone()
    return {
        "seller_status": seller["status"] if seller else None,
        "week_spend": week_spend(conn, aid, policy["budget_window_days"]),
        "seller_settled": tuple(r["amount"] for r in settled),
        "duplicate": duplicate is not None,
        "duplicate_of": duplicate["id"] if duplicate else None,
    }


def week_spend(conn: Connection, aid: int, days: int) -> int:
    """Return what this agent (by id) has spent or has in flight over the last days, in atomic units."""
    row = conn.execute(
        f"SELECT (COALESCE(SUM(amount), 0) {ATOMIC})::bigint AS total FROM payment_attempts"
        " WHERE agent_id = %s AND created_at >= now() - make_interval(days => %s) AND status = ANY(%s)",
        (aid, days, LIVE_STATUSES),
    ).fetchone()
    return int(row["total"]) if row else 0


def find_live_id(conn: Connection, agent: str, quote: dict[str, Any]) -> int | None:
    """Return the id of the live (approved, not yet settled) attempt holding the in flight lock for this payment.

    Lets a repeated request get the original request id back instead of a new row.
    """
    row = conn.execute(
        "SELECT id FROM payment_attempts WHERE status = 'approved' AND dedupe_key = %s",
        (f"{agent}|{quote['pay_to'].lower()}|{quote['url']}|{quote['amount']}",),
    ).fetchone()
    return row["id"] if row else None


def get_attempt(conn: Connection, attempt_id: int, agent: str) -> dict[str, Any] | None:
    """Return one attempt as a dict, or None if it does not exist or belongs to another agent.

    Scoped to the calling agent so one agent can never read another's payments. Amount is in atomic units.
    """
    return conn.execute(
        f"SELECT a.id, a.status, a.url, a.pay_to, (a.amount {ATOMIC})::bigint AS amount, a.decision, a.rule_fired,"
        " a.tx_hash, a.reason FROM payment_attempts a WHERE a.id = %s AND a.agent_id = %s",
        (attempt_id, agent_id(conn, agent)),
    ).fetchone()


def load_result(conn: Connection, attempt_id: int) -> str | None:
    """Return the stored body of a settled attempt, or None if nothing was stored."""
    row = conn.execute("SELECT body FROM attempt_results WHERE attempt_id = %s", (attempt_id,)).fetchone()
    return row["body"] if row else None
