"""Tasks (mandates) and the tier 1 cache, in the database. Pure functions on a connection.

mcp_server.py creates a mandate when the agent calls start_task; it is live at once with its full budget (nobody
confirms it). payments.review_intent reads it and the session facts intent.tier0 needs; the account pages (web/app.py)
list tasks and let the owner revoke one (a kill switch for the whole task, not a decision on any one payment). Every
lookup that takes an id from outside also takes the agent id read from the caller's identity or session, so one user
can never read or change another's task. Amounts go in and out as integer atomic units.
"""
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from sentinel import guard, intent
from sentinel.attempts import ATOMIC, LIVE_STATUSES
from sentinel.db import Connection, usdc

TASK_LIMIT = 50  # tasks shown on the account page


class TooManyTasks(Exception):
    """The agent started more tasks today than policy.json's intent_tasks_per_day allows."""


def create(
    conn: Connection, agent_id: int, task_text: str, goal: str, expected_types: Sequence[str],
    out_of_scope: Sequence[str], budget: int, per_call: int | None, policy: dict[str, int] = guard.POLICY,
) -> int:
    """Store a live mandate for this agent and return its id. Raises TooManyTasks past the daily limit."""
    started = conn.execute(
        "SELECT count(*) AS n FROM mandates WHERE agent_id = %s AND created_at >= now() - interval '1 day'",
        (agent_id,),
    ).fetchone()
    if started is not None and started["n"] >= policy["intent_tasks_per_day"]:
        raise TooManyTasks
    row = conn.execute(
        "INSERT INTO mandates (agent_id, task_text, goal, expected_types, out_of_scope, budget, per_call, expires_at,"
        " status, confirmed_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s), 'confirmed', now()) RETURNING id",
        (agent_id, task_text, goal, list(expected_types), list(out_of_scope), usdc(budget),
         usdc(per_call) if per_call is not None else None, policy["intent_mandate_ttl_s"]),
    ).fetchone()
    assert row is not None
    return int(row["id"])


COLUMNS = (
    f"m.id, m.agent_id, m.task_text, m.goal, m.expected_types, m.out_of_scope, (m.budget {ATOMIC})::bigint AS budget,"
    f" (m.per_call {ATOMIC})::bigint AS per_call, m.status, m.created_at, m.confirmed_at, m.expires_at,"
    " m.expires_at <= now() AS expired, m.finished_at"
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
        f"SELECT (COALESCE((SELECT SUM(amount) FROM payment_attempts"
        f" WHERE mandate_id = %s AND status = ANY(%s)), 0) {ATOMIC})::bigint"
        " + COALESCE((SELECT SUM(COALESCE(actual_charge, max_charge)) FROM apify_runs"
        " WHERE mandate_id = %s), 0)::bigint AS total",
        (mandate_id, LIVE_STATUSES, mandate_id),
    ).fetchone()
    return int(row["total"]) if row else 0


def session_facts(
    conn: Connection, mandate_id: int, agent_id: int, quote: guard.Quote, policy: dict[str, int] = guard.POLICY,
) -> intent.Session:
    """Read what intent.tier0 and the judge need about this task and this payment. Time windows use the DB clock."""
    counts = conn.execute(
        # A refunded (voided) escrow is not counted: a retry after a bad delivery is not the agent racing.
        "SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 minute' AND status <> 'voided')"
        " AS last_minute,"
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
        purchases=tuple(intent.Purchase(r["url"], int(r["amount"]), r["status"], r["reason"]) for r in history),
        listing=listing_for(conn, mandate_id, quote.url),
        comparison=comparison_for(conn, mandate_id, quote.url, policy["compare_ttl_s"]),
    )


# Price comparisons (compare.py, migration 016)

COMPARISON_LIMIT = 50  # shown on the Tasks page


def save_comparison(
    conn: Connection, mandate_id: int, query: str, calls: int, results: int, offers: list[dict[str, Any]],
    buyable_keys: Sequence[str], best: tuple[str, str, int] | None, cheapest: tuple[str, str, int] | None,
    explanation: str,
) -> int:
    """Store one comparison: every offer as shown, the keys pay() could buy, the best and cheapest (key, name, cost)."""
    row = conn.execute(
        "INSERT INTO price_comparisons (mandate_id, query, calls, results, offers, buyable_keys, best_key, best_name,"
        " best_cost, cheapest_key, cheapest_name, cheapest_cost, explanation)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (mandate_id, query, calls, results, Jsonb(offers), list(buyable_keys),
         best[0] if best else None, best[1] if best else None, usdc(best[2]) if best else None,
         cheapest[0] if cheapest else None, cheapest[1] if cheapest else None,
         usdc(cheapest[2]) if cheapest else None, explanation),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def comparison_for(conn: Connection, mandate_id: int, url: str, ttl_s: int) -> intent.Comparison | None:
    """The newest comparison under this task, younger than ttl_s, that offered this URL (by intent.url_key)."""
    row = conn.execute(
        f"SELECT id, best_key, best_name, cheapest_key, cheapest_name, (cheapest_cost {ATOMIC})::bigint AS cheapest,"
        " calls, explanation FROM price_comparisons WHERE mandate_id = %s AND %s = ANY(buyable_keys)"
        " AND created_at > now() - make_interval(secs => %s) ORDER BY id DESC LIMIT 1",
        (mandate_id, intent.url_key(url), ttl_s),
    ).fetchone()
    if row is None:
        return None
    return intent.Comparison(row["id"], row["best_key"], row["best_name"], row["cheapest_key"], row["cheapest_name"],
                             row["cheapest"], int(row["calls"]), row["explanation"])


def list_comparisons(conn: Connection, agent_id: int, limit: int = COMPARISON_LIMIT) -> list[dict[str, Any]]:
    """This agent's newest comparisons, for the Tasks page, with costs in atomic units."""
    return conn.execute(
        f"SELECT c.id, c.mandate_id, c.query, c.calls, c.results, c.offers, c.best_name,"
        f" (c.best_cost {ATOMIC})::bigint AS best_cost, c.cheapest_name, (c.cheapest_cost {ATOMIC})::bigint AS"
        " cheapest_cost, c.explanation, c.created_at FROM price_comparisons c JOIN mandates m ON m.id = c.mandate_id"
        " WHERE m.agent_id = %s ORDER BY c.id DESC LIMIT %s",
        (agent_id, limit),
    ).fetchall()


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


def endpoint(url: str) -> str:
    """url without its query string or fragment: the endpoint a catalog lists, whatever parameters the agent sets."""
    return urlunsplit(urlsplit(url)._replace(query="", fragment=""))


def listing_for(conn: Connection, mandate_id: int, url: str) -> intent.Listing | None:
    """What the catalog advertised for this URL under this task, or None if it was never listed.

    A catalog lists endpoints (https://seller/funding) and agents call them with parameters
    (https://seller/funding?company=Acme), so a URL with a query string falls back to its endpoint's listing; an exact
    listing wins. The parameters never change the seller, the price or the wallet the listing promised.
    """
    row = conn.execute(
        f"SELECT (listed_amount {ATOMIC})::bigint AS amount, listed_pay_to, name, description FROM listings_seen"
        " WHERE mandate_id = %s AND url = ANY(%s) ORDER BY url = %s DESC LIMIT 1",
        (mandate_id, [url, endpoint(url)], url),
    ).fetchone()
    if row is None:
        return None
    return intent.Listing(row["amount"], row["listed_pay_to"], row["name"], row["description"])


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


WITH_SPENT = (  # spent counts live payments plus Apify runs (atomic units, the run's actual or capped charge)
    f"SELECT {COLUMNS}, (COALESCE(s.total, 0) {ATOMIC})::bigint"
    " + COALESCE((SELECT SUM(COALESCE(actual_charge, max_charge)) FROM apify_runs ar"
    " WHERE ar.mandate_id = m.id), 0)::bigint AS spent FROM mandates m"
    " LEFT JOIN LATERAL (SELECT SUM(amount) AS total FROM payment_attempts p"
    "  WHERE p.mandate_id = m.id AND p.status = ANY(%s)) s ON true"
)


def list_for_agent(conn: Connection, agent_id: int, limit: int = TASK_LIMIT) -> list[dict[str, Any]]:
    """This agent's newest tasks with what each has spent, newest first."""
    rows = conn.execute(
        WITH_SPENT + " WHERE m.agent_id = %s ORDER BY m.id DESC LIMIT %s", (LIVE_STATUSES, agent_id, limit),
    ).fetchall()
    purchases = conn.execute("SELECT * FROM apify_runs WHERE agent_id = %s ORDER BY id DESC LIMIT 100",
                             (agent_id,)).fetchall()
    for purchase in purchases:
        charge = purchase["actual_charge"]
        purchase["charge_usd"] = format(usdc(charge), ".6f") if charge is not None else None
    for row in rows:
        row["apify_purchases"] = [p for p in purchases if p["mandate_id"] == row["id"]]
    return rows


def list_by_ids(conn: Connection, agent_id: int, ids: Sequence[int]) -> list[dict[str, Any]]:
    """These tasks of this agent with what each has spent, in one query (ids of other agents are ignored)."""
    if not ids:
        return []
    return conn.execute(
        WITH_SPENT + " WHERE m.agent_id = %s AND m.id = ANY(%s)", (LIVE_STATUSES, agent_id, list(ids)),
    ).fetchall()


def finish(conn: Connection, mandate_id: int, agent_id: int) -> None:
    """The agent sent its final answer for this task: the Live page shows it as done. Display only; once."""
    conn.execute(
        "UPDATE mandates SET finished_at = now() WHERE id = %s AND agent_id = %s AND finished_at IS NULL",
        (mandate_id, agent_id),
    )


def revoke(conn: Connection, mandate_id: int, agent_id: int) -> None:
    """The owner stops one of their tasks; nothing more can be bought for it. ValueError if it is not theirs."""
    row = conn.execute(
        "UPDATE mandates SET status = 'revoked' WHERE id = %s AND agent_id = %s AND status <> 'revoked' RETURNING id",
        (mandate_id, agent_id),
    ).fetchone()
    if row is None:
        raise ValueError("That task is already revoked.")
