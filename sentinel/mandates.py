"""Tasks (mandates), the tier 1 cache and the owner's Allow once, in the database. Pure functions on a connection.

mcp_server.py creates a mandate when the agent calls start_task; payments.review_intent reads it and the session facts
intent.tier0 needs; the account pages (web/app.py) list, confirm and revoke tasks and allow held payments. Every
lookup that takes an id from outside also takes the agent id read from the caller's identity or session, so one user
can never read or change another's task or held payment. Amounts go in and out as integer atomic units.
"""
from collections.abc import Sequence
from typing import Any

from sentinel import guard, intent
from sentinel.attempts import ATOMIC, LIVE_STATUSES
from sentinel.db import Connection, usdc

TASK_LIMIT = 50  # tasks shown on the account page
PREVIEW_CHARS = 600  # how much of a held delivery the Tasks page shows
HELD_LIMIT = 50


class TooManyTasks(Exception):
    """The agent started more tasks today than policy.json's intent_tasks_per_day allows."""


def create(
    conn: Connection, agent_id: int, task_text: str, goal: str, expected_types: Sequence[str],
    out_of_scope: Sequence[str], budget: int, per_call: int | None, policy: dict[str, int] = guard.POLICY,
) -> int:
    """Store a provisional mandate for this agent and return its id. Raises TooManyTasks past the daily limit."""
    started = conn.execute(
        "SELECT count(*) AS n FROM mandates WHERE agent_id = %s AND created_at >= now() - interval '1 day'",
        (agent_id,),
    ).fetchone()
    if started is not None and started["n"] >= policy["intent_tasks_per_day"]:
        raise TooManyTasks
    row = conn.execute(
        "INSERT INTO mandates (agent_id, task_text, goal, expected_types, out_of_scope, budget, per_call, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
        (agent_id, task_text, goal, list(expected_types), list(out_of_scope), usdc(budget),
         usdc(per_call) if per_call is not None else None, policy["intent_mandate_ttl_s"]),
    ).fetchone()
    assert row is not None
    return int(row["id"])


COLUMNS = (
    f"m.id, m.agent_id, m.task_text, m.goal, m.expected_types, m.out_of_scope, (m.budget {ATOMIC})::bigint AS budget,"
    f" (m.per_call {ATOMIC})::bigint AS per_call, m.status, m.created_at, m.confirmed_at, m.expires_at,"
    " m.expires_at <= now() AS expired"
)


def get(conn: Connection, mandate_id: int, agent_id: int) -> dict[str, Any] | None:
    """One of this agent's mandates as a dict (amounts in atomic units), or None if it is not theirs."""
    return conn.execute(
        f"SELECT {COLUMNS} FROM mandates m WHERE m.id = %s AND m.agent_id = %s", (mandate_id, agent_id)
    ).fetchone()


def to_mandate(row: dict[str, Any]) -> intent.Mandate:
    """A mandate row from get() as the intent.Mandate the rules read."""
    return intent.Mandate(
        id=row["id"], task_text=row["task_text"], goal=row["goal"], expected_types=tuple(row["expected_types"]),
        out_of_scope=tuple(row["out_of_scope"]), budget=row["budget"], per_call=row["per_call"],
        status=row["status"], expired=bool(row["expired"]),
    )


def spend(conn: Connection, mandate_id: int) -> int:
    """What this task has spent or has in flight, in atomic units."""
    row = conn.execute(
        f"SELECT (COALESCE(SUM(amount), 0) {ATOMIC})::bigint AS total FROM payment_attempts"
        " WHERE mandate_id = %s AND status = ANY(%s)",
        (mandate_id, LIVE_STATUSES),
    ).fetchone()
    return int(row["total"]) if row else 0


def session_facts(
    conn: Connection, mandate_id: int, agent_id: int, quote: guard.Quote, policy: dict[str, int] = guard.POLICY,
) -> intent.Session:
    """Read what intent.tier0 and the judge need about this task and this payment. Time windows use the DB clock."""
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 minute') AS last_minute,"
        " count(*) FILTER (WHERE url = %s AND status = ANY(%s)) AS url_bought"
        " FROM payment_attempts WHERE mandate_id = %s AND preview IS NOT TRUE",  # searches are not pay calls
        (quote.url, LIVE_STATUSES, mandate_id),
    ).fetchone()
    seller = conn.execute(
        f"SELECT (COALESCE(SUM(amount), 0) {ATOMIC})::bigint AS total FROM payment_attempts"
        " WHERE agent_id = %s AND lower(pay_to) = lower(%s) AND status = ANY(%s)"
        " AND created_at >= now() - make_interval(secs => %s)",
        (agent_id, quote.pay_to, LIVE_STATUSES, policy["intent_split_window_s"]),
    ).fetchone()
    tiers = conn.execute(
        "SELECT intent_tier FROM payment_attempts WHERE mandate_id = %s AND intent_tier IS NOT NULL"
        " AND preview IS NOT TRUE ORDER BY id DESC LIMIT %s",
        (mandate_id, policy["intent_drift_window"]),
    ).fetchall()
    bad = conn.execute(
        "SELECT count(*) AS n FROM payment_attempts WHERE agent_id = %s AND lower(pay_to) = lower(%s)"
        " AND delivery_verdict = 'fail' AND created_at >= now() - make_interval(days => %s)",
        (agent_id, quote.pay_to, policy["budget_window_days"]),
    ).fetchone()
    history = conn.execute(
        f"SELECT url, (amount {ATOMIC})::bigint AS amount, status, reason FROM payment_attempts"
        " WHERE mandate_id = %s AND amount IS NOT NULL AND preview IS NOT TRUE ORDER BY id DESC LIMIT %s",
        (mandate_id, intent.MAX_HISTORY),
    ).fetchall()
    return intent.Session(
        spend=spend(conn, mandate_id),
        last_minute=int(counts["last_minute"]) if counts else 0,
        url_bought=int(counts["url_bought"]) if counts else 0,
        seller_window_spend=int(seller["total"]) if seller else 0,
        recent_tiers=tuple(int(r["intent_tier"]) for r in tiers),
        allowance_id=find_allowance(conn, mandate_id, quote),
        purchases=tuple(intent.Purchase(r["url"], int(r["amount"]), r["status"], r["reason"]) for r in history),
        listing=listing_for(conn, mandate_id, quote.url),
        seller_bad_deliveries=int(bad["n"]) if bad else 0,
    )


def remember_listings(conn: Connection, mandate_id: int, listings: Sequence[Any]) -> None:
    """Store what the catalog advertised for each URL shown under this task (discovery.Listing rows), newest wins."""
    for item in listings:
        conn.execute(
            "INSERT INTO listings_seen (mandate_id, url, listed_amount, listed_pay_to, name, description)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (mandate_id, url) DO UPDATE SET"
            " listed_amount = excluded.listed_amount, listed_pay_to = excluded.listed_pay_to, name = excluded.name,"
            " description = excluded.description, seen_at = now()",
            (mandate_id, item.url, usdc(item.listed_amount) if item.listed_amount is not None else None,
             item.listed_pay_to, item.name or None, item.description or None),
        )


def listing_for(conn: Connection, mandate_id: int, url: str) -> intent.Listing | None:
    """What the catalog advertised for this URL under this task, or None if it was never listed."""
    row = conn.execute(
        f"SELECT (listed_amount {ATOMIC})::bigint AS amount, listed_pay_to, name, description FROM listings_seen"
        " WHERE mandate_id = %s AND url = %s",
        (mandate_id, url),
    ).fetchone()
    if row is None:
        return None
    return intent.Listing(row["amount"], row["listed_pay_to"], row["name"], row["description"])


def find_allowance(conn: Connection, mandate_id: int, quote: guard.Quote) -> int | None:
    """An unused, unexpired Allow once on a held payment under this task with the same seller, URL and amount."""
    row = conn.execute(
        "SELECT al.id FROM allowances al JOIN payment_attempts a ON a.id = al.attempt_id"
        " WHERE a.mandate_id = %s AND lower(a.pay_to) = lower(%s) AND a.url = %s AND a.amount = %s"
        " AND al.used_at IS NULL AND al.expires_at > now() ORDER BY al.id LIMIT 1",
        (mandate_id, quote.pay_to, quote.url, usdc(quote.amount)),
    ).fetchone()
    return int(row["id"]) if row else None


def use_allowance(conn: Connection, allowance_id: int, attempt_id: int) -> bool:
    """Spend an Allow once on the payment it let through. False if it was already used (two retries at once)."""
    row = conn.execute(
        "UPDATE allowances SET used_at = now(), used_by_attempt_id = %s WHERE id = %s AND used_at IS NULL RETURNING id",
        (attempt_id, allowance_id),
    ).fetchone()
    return row is not None


def cached(conn: Connection, mandate_id: int, pay_to: str, url: str, ttl_s: int) -> tuple[str, str] | None:
    """The tier 1 (verdict, reason) for this task, seller and URL, if one is younger than ttl_s."""
    row = conn.execute(
        "SELECT verdict, reason FROM intent_cache WHERE mandate_id = %s AND pay_to = lower(%s) AND url = %s"
        " AND created_at > now() - make_interval(secs => %s)",
        (mandate_id, pay_to, url, ttl_s),
    ).fetchone()
    return (row["verdict"], row["reason"]) if row else None


def remember(conn: Connection, mandate_id: int, pay_to: str, url: str, verdict: str, reason: str) -> None:
    """Store (or refresh) the tier 1 verdict for this task, seller and URL."""
    conn.execute(
        "INSERT INTO intent_cache (mandate_id, pay_to, url, verdict, reason) VALUES (%s, lower(%s), %s, %s, %s)"
        " ON CONFLICT (mandate_id, pay_to, url) DO UPDATE"
        " SET verdict = excluded.verdict, reason = excluded.reason, created_at = now()",
        (mandate_id, pay_to, url, verdict, reason),
    )


# Account pages


def list_for_agent(conn: Connection, agent_id: int, limit: int = TASK_LIMIT) -> list[dict[str, Any]]:
    """This agent's newest tasks with what each has spent, the provisional ones first."""
    rows = conn.execute(
        f"SELECT {COLUMNS}, (COALESCE(s.total, 0) {ATOMIC})::bigint AS spent FROM mandates m"
        " LEFT JOIN LATERAL (SELECT SUM(amount) AS total FROM payment_attempts p"
        "  WHERE p.mandate_id = m.id AND p.status = ANY(%s)) s ON true"
        " WHERE m.agent_id = %s ORDER BY m.id DESC LIMIT %s",
        (LIVE_STATUSES, agent_id, limit),
    ).fetchall()
    rows.sort(key=lambda r: (r["status"] != intent.PROVISIONAL or r["expired"], -r["id"]))
    return rows


def confirm(conn: Connection, mandate_id: int, agent_id: int, budget: int, weekly_budget: int) -> None:
    """The owner confirms one of their provisional tasks, with a budget of at most their weekly budget.

    Raises ValueError with a message the page can show.
    """
    if budget <= 0:
        raise ValueError("The budget must be more than $0.00.")
    if budget > weekly_budget:
        raise ValueError(f"A task's budget can be at most your weekly budget, {guard.usd(weekly_budget)}.")
    row = conn.execute(
        "UPDATE mandates SET status = 'confirmed', budget = %s, confirmed_at = now()"
        " WHERE id = %s AND agent_id = %s AND status = 'provisional' AND expires_at > now() RETURNING id",
        (usdc(budget), mandate_id, agent_id),
    ).fetchone()
    if row is None:
        raise ValueError("That task cannot be confirmed: it was revoked, has expired or is already confirmed.")


def revoke(conn: Connection, mandate_id: int, agent_id: int) -> None:
    """The owner stops one of their tasks; nothing more can be bought for it. ValueError if it is not theirs."""
    row = conn.execute(
        "UPDATE mandates SET status = 'revoked' WHERE id = %s AND agent_id = %s AND status <> 'revoked' RETURNING id",
        (mandate_id, agent_id),
    ).fetchone()
    if row is None:
        raise ValueError("That task is already revoked.")


def count_pending(conn: Connection, agent_id: int) -> dict[str, int]:
    """For the overview banner: provisional live tasks and held payments still waiting for the owner."""
    tasks = conn.execute(
        "SELECT count(*) AS n FROM mandates WHERE agent_id = %s AND status = 'provisional' AND expires_at > now()",
        (agent_id,),
    ).fetchone()
    return {"tasks": int(tasks["n"]) if tasks else 0, "held": len(list_held(conn, agent_id)),
            "escrow": len(list_escrow_held(conn, agent_id))}


def list_held(conn: Connection, agent_id: int, limit: int = HELD_LIMIT) -> list[dict[str, Any]]:
    """Held payments of this agent that the owner has not allowed yet, under tasks that are still live, newest first.

    A held payment drops off once allowed, or once the same payment was later paid or blocked anyway.
    """
    rows = conn.execute(
        f"SELECT a.id, a.created_at, a.url, a.pay_to, (a.amount {ATOMIC})::bigint AS amount, a.reason,"
        " a.rule_fired, a.intent_tier, a.mandate_id, m.goal FROM payment_attempts a"
        " JOIN mandates m ON m.id = a.mandate_id"
        " WHERE a.agent_id = %s AND a.status = 'escalated' AND m.status <> 'revoked' AND m.expires_at > now()"
        " AND NOT EXISTS (SELECT 1 FROM allowances al WHERE al.attempt_id = a.id)"
        " AND NOT EXISTS (SELECT 1 FROM payment_attempts b WHERE b.mandate_id = a.mandate_id AND b.id > a.id"
        "  AND lower(b.pay_to) = lower(a.pay_to) AND b.url = a.url AND b.status IN ('settled', 'approved', 'escrowed'))"
        " ORDER BY a.id DESC LIMIT %s",
        (agent_id, limit),
    ).fetchall()
    for r in rows:
        r["rule"], _, r["detail"] = (r.pop("rule_fired") or "").partition(": ")
    return rows


def list_escrow_held(conn: Connection, agent_id: int, limit: int = HELD_LIMIT) -> list[dict[str, Any]]:
    """Payments of this agent held in escrow for the owner, soonest deadline first, each with the start of what the
    seller delivered (preview) so the owner can judge it."""
    return conn.execute(
        f"SELECT a.id, a.created_at, a.url, a.pay_to, (a.amount {ATOMIC})::bigint AS amount, a.reason,"
        " e.verdict_reason AS detail, e.capture_deadline, m.goal, left(r.body, %s) AS preview"
        " FROM escrow_payments e JOIN payment_attempts a ON a.id = e.attempt_id"
        " LEFT JOIN mandates m ON m.id = a.mandate_id LEFT JOIN attempt_results r ON r.attempt_id = a.id"
        " WHERE a.agent_id = %s AND a.status = 'escrowed' AND e.state = 'authorized' AND e.held"
        " ORDER BY e.capture_deadline LIMIT %s",
        (PREVIEW_CHARS, agent_id, limit),
    ).fetchall()


def allow_once(conn: Connection, attempt_id: int, agent_id: int, user_id: str, policy: dict[str, int] = guard.POLICY,
               ) -> None:
    """The owner allows one held payment of their own agent. ValueError if it is not a payment they can allow."""
    held = conn.execute(
        "SELECT a.id FROM payment_attempts a JOIN mandates m ON m.id = a.mandate_id"
        " WHERE a.id = %s AND a.agent_id = %s AND a.status = 'escalated' AND m.status <> 'revoked'"
        " AND m.expires_at > now()",
        (attempt_id, agent_id),
    ).fetchone()
    if held is None:
        raise ValueError("That payment cannot be allowed: it is not held, or its task was revoked or has expired.")
    row = conn.execute(
        "INSERT INTO allowances (attempt_id, granted_by, expires_at)"
        " VALUES (%s, %s, now() + make_interval(secs => %s)) ON CONFLICT (attempt_id) DO NOTHING RETURNING id",
        (attempt_id, user_id, policy["intent_allowance_ttl_s"]),
    ).fetchone()
    if row is None:
        raise ValueError("You already allowed that payment.")
