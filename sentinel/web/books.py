"""What a signed in user sees and changes about their own agent: sellers, payments, journal and totals.

The account pages (web/app.py) call these functions with the agent id read from the database for the session's user,
never one taken from a request, so every query here is scoped to that one agent. Sellers come from two lists: the
operator's (sellers.agent_id null, applies to every agent, owner.py keeps it) and the user's own (agent_id = this
agent). Any applicable blocked row wins, then any approved row, the same rule attempts.load_history gives the guard.
A user adds, blocks and re-approves only their own rows, so no user can open a seller for anybody else or undo an
operator block. Pure functions on a connection, tested like owner.py. Amounts are integer atomic units; the pages
format them with the usdc filter. Never floats.
"""
import hashlib
import os
from collections.abc import Mapping
from typing import Any

from sentinel import attempts, guard, mandates, owner
from sentinel.db import Connection
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA

USER = "user"  # sellers.added_by for rows a user listed on their account page
PAYMENT_LIMIT = 200
CSV_LIMIT = 20_000  # the most rows the payments CSV holds (built in memory)
SELLER_LIMIT = 200  # the most personal seller rows one user may keep
JOURNAL_LIMIT = 100
RANK = {"blocked": 0, "approved": 1}  # a lower rank wins; anything else (pending) ranks last
DEMO_SELLERS = (("DataCo", "SELLER_A_ADDRESS"), ("InsightLab", "SELLER_B_ADDRESS"))  # never seller C, the scammer
# The demo marketplace's PocketWiFi seller (demo/sellers.py) is paid at a wallet that has never been used, derived from
# a fixed phrase so the marketplace and the Rules card agree. Approving it shows the "no activity" warning; paying it
# is held (unused_wallet). Once one payment to it is allowed and settles, it has activity and stops being fresh.
DEMO_FRESH_WALLET = "0x" + hashlib.sha256(b"sentinel demo: a wallet that has never been used").hexdigest()[:40]
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
    confirmed, revoked or ended), the rows (newest first) and counts of paid, blocked, held and found while searching.
    """
    rows = list_payments(conn, agent_id, limit)
    tasks = {t["id"]: t for t in mandates.list_for_agent(conn, agent_id, limit)}
    groups: dict[int | None, dict[str, Any]] = {}
    for row in rows:  # newest first, so groups come out in the order of their newest payment
        task_id = row.get("task_id")
        group = groups.get(task_id)
        if group is None:
            task = tasks.get(task_id) if task_id is not None else None
            if task_id is not None and task is None:  # an older task than the list reaches
                task = mandates.get(conn, task_id, agent_id)
                if task is not None:
                    task = {**task, "spent": mandates.spend(conn, task_id)}
            state = None
            if task is not None:
                state = "ended" if task["expired"] and task["status"] != "revoked" else task["status"]
            group = groups[task_id] = {"task_id": task_id, "task": task, "state": state, "rows": [],
                                       "paid": 0, "blocked": 0, "held": 0, "searched": 0}
        group["rows"].append(row)
        group["paid"] += row["status"] == "settled"
        group["blocked"] += row["status"] == "blocked"
        group["held"] += row["status"] == "escalated"
        group["searched"] += bool(row.get("found_while_searching"))
    ordered = [g for key, g in groups.items() if key is not None]
    if None in groups:
        ordered.append(groups[None])  # payments outside any task last
    return ordered


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


def demo_sellers(
    conn: Connection, agent_id: int, dev_mode: bool, env: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The local demo sellers (DataCo, InsightLab, PocketWiFi) this agent has not approved yet, for the dev only Rules
    card.

    Empty outside dev mode. DataCo and InsightLab come from the environment at request time (SELLER_A_ADDRESS and
    SELLER_B_ADDRESS); PocketWiFi is DEMO_FRESH_WALLET, a wallet with no history, so approving it shows the warning.
    The scammer's address is never listed. An entry is hidden once the agent approves it.
    """
    if not dev_mode:
        return []
    source = os.environ if env is None else env
    approved = {s["pay_to"].lower() for s in list_sellers(conn, agent_id) if s["status"] == "approved"}
    found = []
    candidates = [(name, (source.get(var) or "").strip()) for name, var in DEMO_SELLERS]
    candidates.append(("PocketWiFi (new wallet)", DEMO_FRESH_WALLET))
    for name, address in candidates:
        if owner.ADDRESS.fullmatch(address) and address.lower() not in approved:
            found.append({"name": name, "pay_to": address})
    return found
