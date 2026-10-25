"""What a signed in user sees and changes about their own agent: sellers, payments, journal and totals.

The account pages (web/app.py) call these functions with the agent id read from the database for the session's user,
never one taken from a request, so every query here is scoped to that one agent. Sellers come from two lists: the
operator's (sellers.agent_id null, applies to every agent, owner.py keeps it) and the user's own (agent_id = this
agent). Any applicable blocked row wins, then any approved row, the same rule attempts.load_history gives the guard.
A user adds, blocks and re-approves only their own rows, so no user can open a seller for anybody else or undo an
operator block. Pure functions on a connection, tested like owner.py. Amounts are integer atomic units; the pages
format them with the usdc filter. Never floats.
"""
from typing import Any

from sentinel import attempts, guard, live, mandates, owner
from sentinel.db import Connection
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA

USER = "user"  # sellers.added_by for rows a user listed on their account page
PAYMENT_LIMIT = 200
CSV_LIMIT = 20_000  # the most rows the payments CSV holds (built in memory)
SELLER_LIMIT = 200  # the most personal seller rows one user may keep
JOURNAL_LIMIT = 100
LIVE_LIMIT = 30  # attempts on the Live page's feed
ESCROW_LIMIT = 50  # escrows listed on the Live and Tasks pages
# Statuses of attempts Sentinel did not let through: blocked, denied, escalated (older rows), refunded (voided), and
# the old approval flow's rows that were never paid. A preview (a service turned away during a search) counts too.
STOPPED = ("blocked", "denied", "escalated", "voided", "pending", "authorized", "cancelled", "expired")
RANK = {"blocked": 0, "approved": 1}  # a lower rank wins; anything else (pending) ranks last
# The journal entries of one agent: the entries of its settled payments and the reversals of those entries.
ENTRY_SCOPE = (
    "(e.attempt_id IN (SELECT id FROM payment_attempts WHERE agent_id = %(aid)s)"
    " OR e.reverses_entry_id IN (SELECT e2.id FROM journal_entries e2 JOIN payment_attempts p ON p.id = e2.attempt_id"
    " WHERE p.agent_id = %(aid)s))"
)


def _rank(status: str) -> int:
    """Sort key for seller statuses: blocked first, then approved, then the rest."""
    return RANK.get(status, 2)


def list_sellers(conn: Connection, agent_id: int) -> list[dict[str, Any]]:
    """The sellers that apply to this agent, one row per address, approved first and then by name.

    Each row has name, pay_to, status (the winner: blocked beats approved), source ("you" when the user's own row
    decides or ties with the operator's approval, "operator" when only the operator's row decides, which includes any
    operator block), own_status (the status of the user's own row, or None) and own_listed_at.
    The operator's rows cannot be changed from here.
    """
    columns = (
        "SELECT id, name, pay_to, status, agent_id, added_at FROM sellers"
        " WHERE network = %s AND lower(asset) = lower(%s)"
    )
    rows = conn.execute(
        columns + " AND agent_id IS NULL ORDER BY id", (NETWORK, USDC_BASE_SEPOLIA)
    ).fetchall() + conn.execute(
        columns + " AND agent_id = %s ORDER BY id LIMIT %s", (NETWORK, USDC_BASE_SEPOLIA, agent_id, SELLER_LIMIT)
    ).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        mine = r["agent_id"] is not None
        entry = merged.setdefault(r["pay_to"].lower(), {"own": None, "operator": None})
        entry["own" if mine else "operator"] = r
    result = []
    for entry in merged.values():
        own, operator = entry["own"], entry["operator"]
        # Only the operator's block is binding, so when both block, the operator is the source.
        if own is not None and (operator is None or (
            operator["status"] != "blocked" and _rank(own["status"]) <= _rank(operator["status"])
        )):
            winner, source = own, "you"
        else:
            winner, source = operator, "operator"
        result.append({
            "name": winner["name"], "pay_to": winner["pay_to"], "status": winner["status"], "source": source,
            "own_status": own["status"] if own else None, "own_listed_at": own["added_at"] if own else None,
        })
    result.sort(key=lambda s: (s["status"] != "approved", s["name"].lower(), s["pay_to"].lower()))
    return result


def upsert_seller(conn: Connection, agent_id: int, name: str, pay_to: str, status: str = "approved") -> int:
    """Approve or block a seller on this agent's own list, adding it if new. Returns the seller id.

    Raises ValueError with a message the page can show: no name, not an address, a status other than approved or
    blocked, or the user's own wallet address. Only the user's own row is created or changed, even when the operator
    lists the same address. Matches the address case insensitively, as the guard does.
    """
    name, pay_to = name.strip(), pay_to.strip()
    if not name:
        raise ValueError("The seller needs a name.")
    if len(name) > 100:
        raise ValueError("The name can be at most 100 characters.")
    if not owner.ADDRESS.fullmatch(pay_to):
        raise ValueError("That is not a wallet address. It is 0x followed by 40 hex characters.")
    if status not in owner.SELLER_STATUSES:
        raise ValueError("The status must be approved or blocked.")
    wallet = conn.execute("SELECT wallet_address FROM users WHERE agent_id = %s", (agent_id,)).fetchone()
    if wallet is not None and (wallet["wallet_address"] or "").lower() == pay_to.lower():
        raise ValueError("That is your own wallet. Sentinel pays sellers, not you.")
    counted = conn.execute(
        "SELECT count(*) AS n, COALESCE(bool_or(lower(pay_to) = lower(%s) AND network = %s"
        " AND lower(asset) = lower(%s)), false) AS listed FROM sellers WHERE agent_id = %s",
        (pay_to, NETWORK, USDC_BASE_SEPOLIA, agent_id),
    ).fetchone()
    assert counted is not None
    if not counted["listed"] and counted["n"] >= SELLER_LIMIT:
        raise ValueError(
            f"You can keep up to {SELLER_LIMIT} sellers; block one you no longer use instead of adding another."
        )
    # One statement, so two submits at once cannot collide: the index is sellers_agent_key (migration 009).
    row = conn.execute(
        "INSERT INTO sellers (name, pay_to, network, asset, status, added_by, agent_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (agent_id, lower(pay_to), network, lower(asset)) WHERE agent_id IS NOT NULL"
        " DO UPDATE SET name = excluded.name, status = excluded.status, added_by = excluded.added_by, added_at = now()"
        " RETURNING id",
        (name, pay_to, NETWORK, USDC_BASE_SEPOLIA, status, USER, agent_id),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def list_payments(conn: Connection, agent_id: int, limit: int = PAYMENT_LIMIT) -> list[dict[str, Any]]:
    """This agent's newest payments (atomic units), seller name from the lists that apply, rule split from detail."""
    return owner.list_attempts(conn, limit, agent_id)


def payments_by_task(conn: Connection, agent_id: int, limit: int = PAYMENT_LIMIT) -> list[dict[str, Any]]:
    """This agent's newest payments grouped by the task they ran under, the task with the newest payment first.

    Each group: task (the mandates.list_for_agent row, or None for payments made outside a task), state (provisional,
    confirmed, revoked or ended), the rows (newest first) and counts: paid (settled), stopped (every row Sentinel did
    not let through, STOPPED), escrowed (in escrow now) and searched (found while searching).
    """
    rows = list_payments(conn, agent_id, limit)
    tasks = {t["id"]: t for t in mandates.list_for_agent(conn, agent_id, limit)}
    older = {r["task_id"] for r in rows if r.get("task_id") is not None and r["task_id"] not in tasks}
    tasks.update((t["id"], t) for t in mandates.list_by_ids(conn, agent_id, sorted(older)))  # one query, not per task
    groups: dict[int | None, dict[str, Any]] = {}
    for row in rows:  # newest first, so groups come out in the order of their newest payment
        task_id = row.get("task_id")
        group = groups.get(task_id)
        if group is None:
            task = tasks.get(task_id) if task_id is not None else None
            state = None
            if task is not None:
                state = "ended" if task["expired"] and task["status"] != "revoked" else task["status"]
            group = groups[task_id] = {"task_id": task_id, "task": task, "state": state, "rows": [],
                                       "paid": 0, "stopped": 0, "escrowed": 0, "searched": 0}
        group["rows"].append(row)
        group["paid"] += row["status"] == "settled"
        group["stopped"] += row["status"] in STOPPED or bool(row.get("found_while_searching"))
        group["escrowed"] += row["status"] == "escrowed"
        group["searched"] += bool(row.get("found_while_searching"))
    ordered = [g for key, g in groups.items() if key is not None]
    if None in groups:
        ordered.append(groups[None])  # payments outside any task last
    return ordered


def fingerprint(conn: Connection, agent_id: int) -> str:
    """A short tag that changes whenever the Live or Payments page would show something new, in one query.

    Covers the newest PAYMENT_LIMIT attempts (status, delivery check, escrow state), the newest tasks (status,
    budget, expiry), the seller lists that name the sellers, Apify runs (they count toward task spend) and the checks
    in progress. Polls compare tags and skip the page.
    """
    row = conn.execute(
        "SELECT md5(concat_ws('|',"
        " (SELECT string_agg(concat_ws(':', a.id, a.status, a.delivery_verdict, a.tx_hash, e.state, e.held),"
        "  ',' ORDER BY a.id) FROM (SELECT id, status, delivery_verdict, tx_hash FROM payment_attempts"
        "  WHERE agent_id = %(aid)s ORDER BY id DESC LIMIT %(scan)s) a"
        "  LEFT JOIN escrow_payments e ON e.attempt_id = a.id),"
        " (SELECT string_agg(concat_ws(':', m.id, m.status, m.budget, m.expires_at <= now(), m.finished_at IS NULL),"
        "  ',' ORDER BY m.id) FROM (SELECT id, status, budget, expires_at, finished_at FROM mandates"
        "  WHERE agent_id = %(aid)s"
        "  ORDER BY id DESC LIMIT %(tasks)s) m),"
        " (SELECT max(added_at) FROM sellers WHERE agent_id IS NULL OR agent_id = %(aid)s),"
        " (SELECT concat_ws(':', count(*), max(updated_at), count(delivery_verdict)) FROM apify_runs"
        "  WHERE agent_id = %(aid)s),"
        " (SELECT string_agg(concat_ws(':', id, cardinality(stages), attempt_id), ',' ORDER BY id) FROM live_checks"
        "  WHERE agent_id = %(aid)s AND updated_at >= now() - make_interval(secs => %(stale)s))"
        " )) AS tag",
        {"aid": agent_id, "scan": PAYMENT_LIMIT, "tasks": mandates.TASK_LIMIT, "stale": live.STALE_S},
    ).fetchone()
    assert row is not None
    return str(row["tag"])


def live_view(conn: Connection, agent_id: int, limit: int = LIVE_LIMIT) -> dict[str, Any]:
    """What the Live page shows: the newest task with its counts, the money held in escrow now, and the feed.

    task is the newest task (mandates.list_for_agent row) or None. counts, over every attempt of that task:
    requests (every attempt Sentinel processed: pay calls and search or quote checks, any status), paid and
    paid_amount (settled, to sellers_paid sellers), stopped (attempts Sentinel did not let through: STOPPED statuses
    or a preview it turned away) and kept_back (their amounts, atomic units), escrowed (in escrow now), refunded
    (voided) and searched (previews). escrow lists every authorized
    escrow, soonest deadline first. feed is the newest attempts of that task from list_attempts, escrow columns
    included, minus the ones still being checked, merged with the task's Apify credit runs (list_apify_runs, kind
    apify) newest first; Apify runs count in requests, paid (succeeded), stopped (blocked, rejected or a failed
    delivery check) and apify_spent (their actual charges). checking is what Sentinel is working on right now under
    that task (live.current), newest first.
    """
    newest = conn.execute(
        mandates.WITH_SPENT + " WHERE m.agent_id = %s ORDER BY m.id DESC LIMIT 1", (attempts.LIVE_STATUSES, agent_id),
    ).fetchone()
    task_id = newest["id"] if newest is not None else None
    checking = live.current(conn, agent_id, task_id)
    busy = {c["attempt_id"] for c in checking if c["attempt_id"] is not None}
    # Only the current task; an attempt still being worked on shows as its check, not as a half finished card.
    feed = [r for r in owner.list_attempts(conn, limit, agent_id, task_id) if r["id"] not in busy]
    runs = list_apify_runs(conn, agent_id, task_id) if task_id is not None else []
    feed = sorted(feed + runs, key=lambda r: r["created_at"], reverse=True)[:limit]
    counts = {"requests": 0, "paid": 0, "paid_amount": 0, "sellers_paid": 0, "stopped": 0, "kept_back": 0,
              "escrowed": 0, "refunded": 0, "searched": 0, "apify_spent": 0}
    if newest is not None:
        stopped = "(status = ANY(%(stopped)s) OR preview IS TRUE)"
        row = conn.execute(
            "SELECT count(*) AS requests,"
            " count(*) FILTER (WHERE status = 'settled') AS paid,"
            f" (COALESCE(SUM(amount) FILTER (WHERE status = 'settled'), 0) {owner.ATOMIC})::bigint AS paid_amount,"
            " count(DISTINCT lower(pay_to)) FILTER (WHERE status = 'settled') AS sellers_paid,"
            f" count(*) FILTER (WHERE {stopped}) AS stopped,"
            f" (COALESCE(SUM(amount) FILTER (WHERE {stopped}), 0) {owner.ATOMIC})::bigint AS kept_back,"
            " count(*) FILTER (WHERE status = 'escrowed') AS escrowed,"
            " count(*) FILTER (WHERE status = 'voided') AS refunded,"
            " count(*) FILTER (WHERE preview) AS searched"
            " FROM payment_attempts WHERE mandate_id = %(task)s",
            {"stopped": list(STOPPED), "task": newest["id"]},
        ).fetchone()
        counts = {**counts, **{k: int(v) for k, v in (row or {}).items()}}
        for run in runs:
            counts["requests"] += 1
            counts["paid"] += run["status"] in ("SUCCEEDED", "TIMED-OUT") and run["delivery_verdict"] == "pass"
            counts["stopped"] += run["status"] in ("BLOCKED", "REJECTED") or run["delivery_verdict"] == "fail"
            counts["apify_spent"] += run["actual_charge"] or 0
    escrow = escrow_now(conn, agent_id)
    names = {r["id"]: r["seller"] for r in feed}
    for e in escrow:
        e["seller"] = names.get(e["id"])
    state = None
    if newest is not None:
        state = "ended" if newest["expired"] and newest["status"] != "revoked" else newest["status"]
        if newest.get("finished_at") is not None and newest["status"] != "revoked":
            state = "done"
    return {"task": newest, "task_state": state, "counts": counts, "escrow": escrow, "feed": feed,
            "checking": checking}


def list_apify_runs(conn: Connection, agent_id: int, task_id: int, limit: int = LIVE_LIMIT) -> list[dict[str, Any]]:
    """This task's Apify credit runs for the Live feed, newest first: kind apify, seller "Apify · <actor>", and the
    apify_runs columns pages.apify_pipeline reads (amounts in millionths of USD)."""
    rows = conn.execute(
        "SELECT id, actor, url, reason, status, max_charge, actual_charge, rule, detail, intent_tier, intent_reason,"
        " ml_model, ml_score, ml_threshold, delivery_verdict, delivery_reason, created_at FROM apify_runs"
        " WHERE agent_id = %s AND mandate_id = %s ORDER BY id DESC LIMIT %s",
        (agent_id, task_id, limit),
    ).fetchall()
    for row in rows:
        row.update(kind="apify", seller=f"Apify · {row['actor']}", url=row["url"] or f"https://apify.com/{row['actor']}")
    return rows


def escrow_now(conn: Connection, agent_id: int, limit: int = ESCROW_LIMIT) -> list[dict[str, Any]]:
    """This agent's money locked in escrow right now (Sentinel still checking the delivery), soonest deadline first.

    Each row: id, url, pay_to, amount (atomic units), reason, capture_deadline, created_at, goal (the task, or None)
    and the ml columns. Read only: Sentinel captures or refunds each one itself.
    """
    return conn.execute(
        f"SELECT a.id, a.url, a.pay_to, (a.amount {owner.ATOMIC})::bigint AS amount, a.reason, e.capture_deadline,"
        " e.created_at, m.goal, a.ml_model, a.ml_score, a.ml_threshold"
        " FROM escrow_payments e JOIN payment_attempts a ON a.id = e.attempt_id"
        " LEFT JOIN mandates m ON m.id = a.mandate_id AND m.agent_id = a.agent_id"
        " WHERE a.agent_id = %s AND e.state = 'authorized' ORDER BY e.capture_deadline LIMIT %s",
        (agent_id, limit),
    ).fetchall()


def recent_payments(conn: Connection, agent_id: int, count: int = 5) -> list[dict[str, Any]]:
    """The last few payments, for the overview."""
    return owner.list_attempts(conn, count, agent_id)


def journal(conn: Connection, agent_id: int, limit: int = JOURNAL_LIMIT) -> list[dict[str, Any]]:
    """The newest journal entries of this agent's payments (and any reversals), each with its lines."""
    entries = conn.execute(
        f"SELECT e.id, e.date, e.memo, e.attempt_id, e.reverses_entry_id FROM journal_entries e"
        f" WHERE {ENTRY_SCOPE} ORDER BY e.id DESC LIMIT %(limit)s",
        {"aid": agent_id, "limit": limit},
    ).fetchall()
    entries = owner.attach_lines(conn, entries)
    ids = [e["attempt_id"] for e in entries if e["attempt_id"] is not None]
    paid = {}
    if ids:
        paid = {r["id"]: r for r in conn.execute(
            "SELECT p.id, p.url, p.pay_to, p.tx_hash, s.name AS seller FROM payment_attempts p"
            " LEFT JOIN LATERAL (SELECT x.name FROM sellers x WHERE lower(x.pay_to) = lower(p.pay_to)"
            "  AND x.network = %s AND lower(x.asset) = lower(%s) AND (x.agent_id IS NULL OR x.agent_id = p.agent_id)"
            "  ORDER BY (x.agent_id IS NULL), x.id LIMIT 1) s ON true"
            " WHERE p.agent_id = %s AND p.id = ANY(%s)",
            (NETWORK, USDC_BASE_SEPOLIA, agent_id, ids),
        ).fetchall()}
    return [{**e, "attempt": paid.get(e["attempt_id"])} for e in entries]


def totals(conn: Connection, agent_id: int) -> dict[str, Any]:
    """Debit and credit per account over this agent's whole journal, plus the grand totals and a balanced flag.

    rows: code, name, debit, credit in atomic units (accounts the agent never used are left out). Every entry is
    balanced when it is written, so balanced is False only if the books were damaged.
    """
    rows = conn.execute(
        f"SELECT a.code, a.name, (SUM(l.debit) {owner.ATOMIC})::bigint AS debit,"
        f" (SUM(l.credit) {owner.ATOMIC})::bigint AS credit"
        " FROM journal_entries e JOIN journal_lines l ON l.entry_id = e.id JOIN accounts a ON a.id = l.account_id"
        f" WHERE {ENTRY_SCOPE} GROUP BY a.id ORDER BY a.code",
        {"aid": agent_id},
    ).fetchall()
    debit, credit = sum(r["debit"] for r in rows), sum(r["credit"] for r in rows)
    return {"rows": rows, "debit": debit, "credit": credit, "balanced": debit == credit}


def summary(conn: Connection, agent_id: int) -> dict[str, int]:
    """settled (count of settled payments), spent (their total) and week_spend (settled plus in flight, one window)."""
    row = conn.execute(
        f"SELECT count(*) AS settled, (COALESCE(SUM(amount), 0) {owner.ATOMIC})::bigint AS spent"
        " FROM payment_attempts WHERE agent_id = %s AND status = 'settled'",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return {
        "settled": int(row["settled"]), "spent": int(row["spent"]),
        "week_spend": attempts.week_spend(conn, agent_id, guard.POLICY["budget_window_days"]),
    }


def has_live_grant(conn: Connection, user_id: str) -> bool:
    """True when an AI app is connected for this user: an exchanged grant that is not revoked and not expired."""
    row = conn.execute(
        "SELECT 1 FROM oauth_codes WHERE user_id = %s AND used_at IS NOT NULL AND revoked_at IS NULL"
        " AND grant_expires_at > now() LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None
