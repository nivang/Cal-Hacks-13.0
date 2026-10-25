"""What only the owner can do: keep the operator's seller list and each agent's limits, and read the books.

The operator's list is the sellers rows with agent_id null; they apply to every agent. A signed in user keeps a
personal list of their own (web/books.py) and can never touch these rows. The agent has no path here.
mcp_server.py exposes pay and payment_status only; these functions are called by the
ledger page (ledger_app.py), which sits behind the owner's password. No web code in this file, so it is tested
like attempts.py. Amounts go in and out as integer atomic units; parse_usd is the one place a typed dollar
amount becomes an integer.
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sentinel import attempts, results
from sentinel.db import USDC, Connection, insert, usdc
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA

ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units
MAX_USDC = 10**12  # numeric(18,6) holds 12 whole digits
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SELLER_STATUSES = ("approved", "blocked")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # a spreadsheet would run a cell starting with these
CSV_COLUMNS = [
    "id", "created_at", "agent", "reason", "seller", "pay_to", "amount_usdc", "decision", "rule", "detail",
    "status", "tx_hash", "response_hash", "url", "seller_listed_by", "seller_listed_at",
    "task_id", "found_at", "intent_tier", "intent_verdict", "intent_reason", "found_while_searching",
]


def parse_usd(text: str) -> int:
    """Turn a typed dollar amount ("$0.50", "10") into atomic units (500000). Raises ValueError if it is not one.

    Decimal, never float, and at most 6 decimals because that is all USDC has.
    """
    cleaned = text.strip().removeprefix("$").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a dollar amount") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{text!r} must be more than $0")
    if amount >= MAX_USDC:
        raise ValueError(f"{text!r} is larger than the books can hold")
    atomic = amount * USDC
    if atomic != atomic.to_integral_value():
        raise ValueError(f"{text!r} has more than 6 decimals")
    return int(atomic)


def upsert_seller(conn: Connection, name: str, pay_to: str, status: str = "approved") -> int:
    """Approve or block a seller's address, adding it if it is new. Returns the seller id.

    This is the operator's list (agent_id null) and never touches a user's personal rows. Matches case insensitively,
    the same way the guard looks sellers up, so one address is never listed twice.
    added_by and added_at record the owner's latest decision, which the ledger shows as who listed the seller.
    """
    name, pay_to = name.strip(), pay_to.strip()
    if not name:
        raise ValueError("The seller needs a name.")
    if not ADDRESS.match(pay_to):
        raise ValueError(f"{pay_to!r} is not a wallet address (0x followed by 40 hex characters).")
    if status not in SELLER_STATUSES:
        raise ValueError(f"status must be one of {SELLER_STATUSES}")
    row = conn.execute(
        "UPDATE sellers SET name = %s, status = %s, added_by = 'owner', added_at = now()"
        " WHERE agent_id IS NULL AND lower(pay_to) = lower(%s) AND network = %s AND lower(asset) = lower(%s)"
        " RETURNING id",
        (name, status, pay_to, NETWORK, USDC_BASE_SEPOLIA),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return insert(
        conn,
        "INSERT INTO sellers (name, pay_to, network, asset, status, added_by) VALUES (%s, %s, %s, %s, %s, 'owner')",
        (name, pay_to, NETWORK, USDC_BASE_SEPOLIA, status),
    )


def set_agent_limits(conn: Connection, agent: str, per_payment_cap: int, weekly_budget: int) -> None:
    """Set an agent's per payment cap and weekly budget (atomic units). The cap cannot exceed the budget."""
    if per_payment_cap <= 0 or weekly_budget <= 0:
        raise ValueError("Limits must be more than $0.")
    if per_payment_cap > weekly_budget:
        raise ValueError("The per payment cap cannot be more than the weekly budget.")
    updated = conn.execute(
        "UPDATE agents SET per_payment_cap = %s, weekly_budget = %s WHERE name = %s",
        (usdc(per_payment_cap), usdc(weekly_budget), agent),
    ).rowcount
    if updated == 0:
        raise LookupError(f"no agent named {agent}")


def set_auto_buy(conn: Connection, agent: str, on: bool) -> None:
    """Turn the agent's auto buy on or off: on, the recommended best value of a price comparison is bought without
    asking even when it is the pricier option (every other check still runs); off, it is held for the owner."""
    updated = conn.execute("UPDATE agents SET auto_buy_best_value = %s WHERE name = %s", (on, agent)).rowcount
    if updated == 0:
        raise LookupError(f"no agent named {agent}")


def list_sellers(conn: Connection) -> list[dict[str, Any]]:
    """Every seller on the operator's list (agent_id null), approved first, then by name."""
    return conn.execute(
        "SELECT id, name, pay_to, status, added_by, added_at FROM sellers WHERE agent_id IS NULL"
        " ORDER BY status = 'approved' DESC, name, id"
    ).fetchall()


def list_agents(conn: Connection, budget_window_days: int) -> list[dict[str, Any]]:
    """Every agent with its cap, budget and what it spent (or has in flight) inside the budget window, atomic units."""
    rows = conn.execute(
        f"SELECT ag.id, ag.name, (ag.per_payment_cap {ATOMIC})::bigint AS per_payment_cap,"
        f" (ag.weekly_budget {ATOMIC})::bigint AS weekly_budget, ac.code AS expense_code, ac.name AS expense_account"
        " FROM agents ag JOIN accounts ac ON ac.id = ag.expense_account_id ORDER BY ag.name"
    ).fetchall()
    return [{**r, "week_spend": attempts.week_spend(conn, r["id"], budget_window_days)} for r in rows]


def list_attempts(conn: Connection, limit: int = 200, agent_id: int | None = None) -> list[dict[str, Any]]:
    """The newest payment attempts with agent, seller name and who listed that seller. Amount in atomic units.

    rule_fired is split into rule and detail. Escrowed attempts carry their escrow row (escrow_state, escrow_held,
    authorize_tx, capture_tx, void_tx, capture_deadline, decided_by), None otherwise. seller is None when the
    address was never on a list that applies to the attempt's agent. When several seller rows share an address, one
    is picked per attempt (the attempt's own agent's row first, then the operator's), so an attempt never repeats.
    agent_id limits the log to one agent.
    """
    rows = conn.execute(
        f"SELECT a.id, a.created_at, ag.name AS agent, a.reason, s.name AS seller, a.pay_to,"
        f" (a.amount {ATOMIC})::bigint AS amount, a.decision, a.rule_fired, a.status, a.tx_hash, a.response_hash,"
        " a.url, s.added_by AS seller_listed_by, s.added_at AS seller_listed_at, a.mandate_id AS task_id, a.found_at,"
        " a.intent_tier, a.intent_verdict, a.intent_reason, COALESCE(a.preview, false) AS found_while_searching,"
        " a.delivery_verdict, a.delivery_reason, e.state AS escrow_state, e.held AS escrow_held,"
        " e.authorize_tx, e.capture_tx, e.void_tx, e.capture_deadline, e.decided_by"
        " FROM payment_attempts a JOIN agents ag ON ag.id = a.agent_id"
        " LEFT JOIN escrow_payments e ON e.attempt_id = a.id"
        " LEFT JOIN LATERAL (SELECT x.name, x.added_by, x.added_at FROM sellers x"
        "  WHERE lower(x.pay_to) = lower(a.pay_to) AND x.network = %s AND lower(x.asset) = lower(%s)"
        "  AND (x.agent_id IS NULL OR x.agent_id = a.agent_id)"
        "  ORDER BY (x.agent_id IS NULL), x.id LIMIT 1) s ON true"
        " WHERE (%s::bigint IS NULL OR a.agent_id = %s::bigint)"
        " ORDER BY a.id DESC LIMIT %s",
        (NETWORK, USDC_BASE_SEPOLIA, agent_id, agent_id, limit),
    ).fetchall()
    for r in rows:
        r["rule"], _, r["detail"] = (r.pop("rule_fired") or "").partition(": ")
    return rows


def list_journal(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    """The newest journal entries, each with its lines (account code, name, debit, credit in atomic units)."""
    entries = conn.execute(
        "SELECT id, date, memo, attempt_id, reverses_entry_id FROM journal_entries ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return attach_lines(conn, entries)


def attach_lines(conn: Connection, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each entry's lines (account code, name, debit, credit in atomic units) as entry["lines"]."""
    if not entries:
        return []
    lines = conn.execute(
        f"SELECT l.entry_id, a.code, a.name, (l.debit {ATOMIC})::bigint AS debit, (l.credit {ATOMIC})::bigint AS credit"
        " FROM journal_lines l JOIN accounts a ON a.id = l.account_id WHERE l.entry_id = ANY(%s)"
        " ORDER BY l.entry_id, l.debit DESC, l.id",
        ([e["id"] for e in entries],),
    ).fetchall()
    by_entry: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        by_entry.setdefault(line.pop("entry_id"), []).append(line)
    return [{**e, "lines": by_entry.get(e["id"], [])} for e in entries]


def safe_cell(value: Any) -> str:
    """Render one CSV cell. Text that a spreadsheet would run as a formula gets a leading apostrophe.

    reason and url come from the agent (and so maybe from a page that fooled it), so they are never trusted.
    """
    text = "" if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def payments_csv(rows: list[dict[str, Any]]) -> str:
    """The payment log from list_attempts as CSV text, one row per attempt, amounts as dollars ("0.50")."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        cells = {**r, "amount_usdc": results.usdc_text(r["amount"]) if r["amount"] is not None else ""}
        writer.writerow([safe_cell(cells.get(column)) for column in CSV_COLUMNS])
    return out.getvalue()
