"""Payment attempt log and the history lookups the guard needs.

Every payment request is written here, including blocked ones, so the ledger shows what
did not happen as well as what did. Sits between payments.py (writes attempts) and guard.py
(reads the facts load_history returns). Amounts go in and out as integer atomic units.
"""
import json
from collections.abc import Mapping
from typing import Any

from sentinel import guard
from sentinel.db import Connection, agent_id, insert, usdc

LIVE_STATUSES = ["settled", "approved", "escrowed"]  # attempts that count as spent, in flight or held in escrow
ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units
# What intent binding records with a payment made under a task (migration 010).
INTENT_COLUMNS = ("mandate_id", "found_at", "seller_description", "intent_tier", "intent_verdict", "intent_reason",
                  "preview")  # preview: a service stopped during a search, nothing attempted (migration 011)


def record_attempt(
    conn: Connection,
    agent: str,
    url: str,
    reason: str | None = None,
    quote: dict[str, Any] | None = None,
    status: str = "blocked",  # fail closed: only an explicit "approved" takes the lock and can settle
    decision: str | None = None,
    rule_fired: str | None = None,
    intent: Mapping[str, Any] | None = None,
) -> int:
    """Insert one payment attempt row and return its id.

    A row with status approved takes the in flight lock for its dedupe key, so a second identical insert
    raises db.UniqueViolation. Blocked and escalated rows never take the lock. intent holds the INTENT_COLUMNS of a
    payment made under a task (payments.review_intent); missing keys stay null.
    """
    dedupe_key = quote and f"{agent}|{quote['pay_to'].lower()}|{url}|{quote['amount']}"
    facts = intent or {}
    return insert(
        conn,
        "INSERT INTO payment_attempts"
        " (agent_id, url, reason, pay_to, amount, quote_json, dedupe_key, decision, rule_fired, status,"
        f" {', '.join(INTENT_COLUMNS)})"
        f" VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, {', '.join(['%s'] * len(INTENT_COLUMNS))})",
        (
            agent_id(conn, agent), url, reason,
            quote and quote["pay_to"], quote and usdc(quote["amount"]), quote and json.dumps(quote),
            dedupe_key, decision, rule_fired, status, *(facts.get(column) for column in INTENT_COLUMNS),
        ),
    )


def preview_logged(conn: Connection, mandate_id: int, url: str, status: str) -> bool:
    """True when a search already logged this URL with this status under this task (so it is logged once)."""
    row = conn.execute(
        "SELECT 1 FROM payment_attempts WHERE preview AND mandate_id = %s AND url = %s AND status = %s LIMIT 1",
        (mandate_id, url, status),
    ).fetchone()
    return row is not None


def set_attempt_status(conn: Connection, attempt_id: int, status: str) -> None:
    """Move an attempt to a new status, e.g. failed. Releases the in flight lock when it leaves approved."""
    conn.execute("UPDATE payment_attempts SET status = %s WHERE id = %s", (status, attempt_id))


def note_delivery(conn: Connection, attempt_id: int, verdict: str, reason: str) -> None:
    """Record the delivery check's verdict on a payment that settled without escrow (nothing can be given back)."""
    conn.execute(
        "UPDATE payment_attempts SET delivery_verdict = %s, delivery_reason = %s WHERE id = %s",
        (verdict, reason, attempt_id),
    )


def block_attempt(conn: Connection, attempt_id: int, rule: str, detail: str) -> None:
    """Record a client refusal before signing, including its reason, and release the in flight lock."""
    conn.execute(
        "UPDATE payment_attempts SET status = 'blocked', decision = 'BLOCK', rule_fired = %s WHERE id = %s",
        (f"{rule}: {detail}", attempt_id),
    )


def load_agent(conn: Connection, name: str) -> dict[str, Any]:
    """Return the agent's name, per payment cap and weekly budget (atomic units) and the owner's auto buy switch
    (auto_buy_best_value, migration 016) as a dict that builds guard.Agent."""
    row = conn.execute(
        f"SELECT name, (per_payment_cap {ATOMIC})::bigint AS per_payment_cap,"
        f" (weekly_budget {ATOMIC})::bigint AS weekly_budget, auto_buy_best_value FROM agents WHERE name = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no agent named {name}")
    return row


def load_history(
    conn: Connection, agent: str, quote: dict[str, Any], policy: dict[str, int],
    exclude_id: int | None = None,
) -> dict[str, Any]:
    """Read the ledger facts guard.evaluate needs for this agent and quote.

    Returns seller_status, week_spend, seller_settled, duplicate, duplicate_of, lookalike_of and url_paid_to as a dict
    that builds guard.History. Spend counts settled and in flight attempts so money on its way out is not double spent.
    seller_settled is this agent's own last settled prices for this product (seller and URL), so the price jump
    baseline is per agent and per product: one user can neither see nor move another's, and a seller's cheap product
    never sets the baseline for its expensive one. lookalike_of is an approved (or the owner's own) address the payee
    imitates; url_paid_to the addresses this agent already paid for this URL. seller_status follows the seller
    lists: the operator's rows (agent_id null) apply to every agent and this agent's own rows to it alone; any
    applicable blocked row wins, then any approved row, then whatever else is listed (pending), else None. So an
    operator block cannot be overridden by a personal approval, and one user's approval never opens a seller for
    another agent.
    Time windows are computed by the database clock.
    """
    aid = agent_id(conn, agent)
    seller = conn.execute(
        "SELECT status FROM sellers WHERE lower(pay_to) = lower(%s) AND network = %s AND lower(asset) = lower(%s)"
        " AND (agent_id IS NULL OR agent_id = %s)"
        " ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, id LIMIT 1",
        (quote["pay_to"], quote["network"], quote["asset"], aid),
    ).fetchone()
    settled = conn.execute(
        f"SELECT (amount {ATOMIC})::bigint AS amount FROM payment_attempts"
        " WHERE status = 'settled' AND agent_id = %s AND lower(pay_to) = lower(%s) AND url = %s"
        " ORDER BY settled_at DESC, id DESC LIMIT %s",
        (aid, quote["pay_to"], quote["url"], policy["price_jump_window"]),
    ).fetchall()
    paid_to = conn.execute(
        "SELECT DISTINCT pay_to FROM payment_attempts WHERE agent_id = %s AND url = %s"
        " AND status IN ('settled', 'escrowed') AND pay_to IS NOT NULL",
        (aid, quote["url"]),
    ).fetchall()
    known = [r["pay_to"] for r in conn.execute(
        "SELECT pay_to FROM sellers WHERE status = 'approved' AND (agent_id IS NULL OR agent_id = %s)"
        " UNION SELECT wallet_address FROM users WHERE agent_id = %s AND wallet_address IS NOT NULL",
        (aid, aid),
    ).fetchall()]
    duplicate = conn.execute(
        "SELECT id FROM payment_attempts WHERE agent_id = %s AND lower(pay_to) = lower(%s) AND url = %s"
        " AND amount = %s AND created_at >= now() - make_interval(secs => %s) AND status = ANY(%s)"
        " AND id <> %s"
        " ORDER BY id DESC LIMIT 1",
        (aid, quote["pay_to"], quote["url"], usdc(quote["amount"]), policy["duplicate_window_s"],
         [*LIVE_STATUSES, "pending", "authorized"], exclude_id or 0),
    ).fetchone()
    return {
        "seller_status": seller["status"] if seller else None,
        "week_spend": week_spend(conn, aid, policy["budget_window_days"]),
        "seller_settled": tuple(r["amount"] for r in settled),
        "duplicate": duplicate is not None,
        "duplicate_of": duplicate["id"] if duplicate else None,
        "lookalike_of": guard.imitates(quote["pay_to"], known),
        "url_paid_to": tuple(r["pay_to"] for r in paid_to),
    }


def seller_statuses(conn: Connection, agent: str, pay_tos: list[str], network: str, asset: str) -> dict[str, str]:
    """What the seller lists say about each address for this agent, keyed by lower case address.

    Same precedence as load_history: any applicable blocked row wins, then approved; addresses on no list, or only
    pending, are left out, so the caller treats them as unlisted.
    """
    rows = conn.execute(
        "SELECT lower(pay_to) AS pay_to, bool_or(status = 'blocked') AS blocked, bool_or(status = 'approved') AS ok"
        " FROM sellers WHERE lower(pay_to) = ANY(%s) AND network = %s AND lower(asset) = lower(%s)"
        " AND (agent_id IS NULL OR agent_id = %s) GROUP BY lower(pay_to)",
        ([p.lower() for p in pay_tos], network, asset, agent_id(conn, agent)),
    ).fetchall()
    return {r["pay_to"]: "blocked" if r["blocked"] else "approved" for r in rows if r["blocked"] or r["ok"]}


def deliveries(conn: Connection, agent: str, keys: list[str]) -> dict[str, tuple[int, int, int]]:
    """What this agent's own purchases of each product delivered: (settled, settled but empty, failed), keyed by the
    URL without its query (intent.url_key). Only this agent's rows, so one user never learns what another bought."""
    rows = conn.execute(
        "SELECT split_part(split_part(a.url, '#', 1), '?', 1) AS key,"
        " count(*) FILTER (WHERE a.status = 'settled') AS settled,"
        " count(*) FILTER (WHERE a.status = 'settled' AND length(btrim(COALESCE(r.body, ''), ' {}[]\"' || chr(10)))"
        "  = 0) AS empty,"
        " count(*) FILTER (WHERE a.status = 'failed') AS failed"
        " FROM payment_attempts a LEFT JOIN attempt_results r ON r.attempt_id = a.id"
        " WHERE a.agent_id = %s AND a.preview IS NOT TRUE AND split_part(split_part(a.url, '#', 1), '?', 1) = ANY(%s)"
        " GROUP BY 1",
        (agent_id(conn, agent), keys),
    ).fetchall()
    return {r["key"]: (int(r["settled"]), int(r["empty"]), int(r["failed"])) for r in rows}


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
        " a.tx_hash, a.reason, a.mandate_id, a.approval_expires_at, a.quote_json, a.delivery_reason,"
        " e.held AS escrow_held, e.void_tx FROM payment_attempts a LEFT JOIN escrow_payments e ON e.attempt_id = a.id"
        " WHERE a.id = %s AND a.agent_id = %s",
        (attempt_id, agent_id(conn, agent)),
    ).fetchone()


def load_result(conn: Connection, attempt_id: int) -> str | None:
    """Return the stored body of a settled attempt, or None if nothing was stored."""
    row = conn.execute("SELECT body FROM attempt_results WHERE attempt_id = %s", (attempt_id,)).fetchone()
    return row["body"] if row else None
