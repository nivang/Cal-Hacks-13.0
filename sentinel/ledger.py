"""Double entry journal: settlements, funding, reversals and the trial balance.

Every entry is balanced and append only. Both rules are enforced by the database (triggers in
migrations/002 and 003), and balance is also checked here first for a clear error. A settlement
updates the attempt and posts its entry in one transaction, so the books never drift from the
payment log. It credits the paying agent's own cash account when it has one (each signed in user's
agent does, 1010-<agent id>) and the shared 1010 USDC Cash otherwise (agents from before sign in, like research-agent).
A payment held in escrow (escrow.py) books in three steps: hold_attempt (Dr escrow / Cr cash), then
capture_attempt (Dr expense / Cr escrow) or void_attempt (Dr cash / Cr escrow), so escrow nets to zero once settled.
Used by payments.py after a payment settles. Amounts are integer atomic units.
"""
import hashlib
import json
from typing import Any

from sentinel.db import Connection, account_id, insert, transaction, usdc

CASH = "1010"  # shared USDC Cash, for agents without a cash account of their own
ESCROW = "1050"  # shared USDC in Escrow; an agent with its own cash account holds in 1050-<agent id>
OWNER_CONTRIBUTIONS = "3000"
ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units


class UnbalancedEntry(ValueError):
    """Raised when an entry's debits do not equal its credits. Nothing is written."""


def sha256_hex(data: bytes | str) -> str:
    """Return the SHA256 of a response body, so we can prove what was received without storing it."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def as_text(body: bytes | str) -> str:
    """Decode a response body for storage. Postgres text cannot hold NUL bytes, so they are replaced.

    The hash is taken over the raw bytes, so this never weakens the proof of what was received.
    """
    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")
    return text.replace("\x00", "\ufffd")


def post_entry(
    conn: Connection,
    memo: str,
    lines: list[tuple[str, int, int]],
    attempt_id: int | None = None,
    reverses: int | None = None,
    step: str = "payment",
) -> int:
    """Write one journal entry and return its id. Call only inside a transaction.

    lines are (account code, debit, credit) in atomic units. Debits must equal credits and be above
    zero, otherwise UnbalancedEntry is raised before anything is written. step says which booking of the attempt this
    is (payment, or an escrow's hold, capture or void); the database allows one of each per attempt.
    """
    debits = sum(debit for _, debit, _ in lines)
    credits = sum(credit for _, _, credit in lines)
    if debits != credits or debits == 0:
        raise UnbalancedEntry(f"debits {debits} != credits {credits}")
    entry_id = insert(
        conn,
        "INSERT INTO journal_entries (attempt_id, reverses_entry_id, memo, step) VALUES (%s, %s, %s, %s)",
        (attempt_id, reverses, memo, step),
    )
    for code, debit, credit in lines:
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, %s, %s)",
            (entry_id, account_id(conn, code), usdc(debit), usdc(credit)),
        )
    return entry_id


def settle_attempt(
    conn: Connection,
    attempt_id: int,
    tx_hash: str,
    response_body: bytes | str,
    content_type: str | None = None,
) -> int:
    """Mark an approved attempt settled and post Dr expense / Cr the agent's cash account in one transaction.

    The cash account is the agent's own (agents.cash_account_id) when it has one, else the shared 1010
    USDC Cash, so each user's payments come out of that user's books. Stores the tx hash, the response
    hash and the body itself (write once, in attempt_results) so the agent can collect what it paid for
    later. All of it commits together or not at all. Returns the journal entry id. Only approved attempts
    can settle, so a blocked or failed attempt can never reach the books. The row is locked while we work
    so two settlers cannot both book the same payment.
    """
    with transaction(conn):
        row = _locked(conn, attempt_id, "approved")
        conn.execute(
            "UPDATE payment_attempts SET status = 'settled', tx_hash = %s, response_hash = %s, settled_at = now()"
            " WHERE id = %s",
            (tx_hash, sha256_hex(response_body), attempt_id),
        )
        _store_body(conn, attempt_id, response_body, content_type)
        return post_entry(
            conn,
            f"x402 payment {row['url']} tx {tx_hash}",
            [(row["expense_code"], row["amount"], 0), (row["cash_code"], 0, row["amount"])],
            attempt_id=attempt_id,
        )


def _locked(conn: Connection, attempt_id: int, status: str) -> dict[str, Any]:
    """Lock an attempt and return its url, amount, agent id and account codes, or raise if it is not in status."""
    row = conn.execute(
        f"SELECT a.url, (a.amount {ATOMIC})::bigint AS amount, a.status, a.agent_id, ac.code AS expense_code,"
        " COALESCE(cash.code, %s) AS cash_code, cash.name AS cash_name"
        " FROM payment_attempts a JOIN agents ag ON ag.id = a.agent_id"
        " JOIN accounts ac ON ac.id = ag.expense_account_id"
        " LEFT JOIN accounts cash ON cash.id = ag.cash_account_id WHERE a.id = %s FOR UPDATE OF a",
        (CASH, attempt_id),
    ).fetchone()
    if row is None or row["status"] != status:
        found = row["status"] if row else "missing"
        raise ValueError(f"attempt {attempt_id} is {found}, not {status}")
    return dict(row)


def _store_body(conn: Connection, attempt_id: int, body: bytes | str, content_type: str | None) -> None:
    conn.execute(
        "INSERT INTO attempt_results (attempt_id, body, content_type) VALUES (%s, %s, %s)",
        (attempt_id, as_text(body), content_type),
    )


def escrow_code(conn: Connection, row: dict[str, Any]) -> str:
    """The escrow account that mirrors this attempt's cash account: 1050-<agent id> beside 1010-<agent id>, created
    the first time that agent holds money, else the shared 1050."""
    if row["cash_code"] == CASH:
        return ESCROW
    code = f"{ESCROW}-{row['agent_id']}"
    name = str(row["cash_name"] or "").replace("USDC Cash", "USDC in Escrow", 1) or f"USDC in Escrow {code}"
    conn.execute(
        "INSERT INTO accounts (code, name, type) VALUES (%s, %s, 'asset') ON CONFLICT (code) DO NOTHING", (code, name)
    )
    return code


def hold_attempt(
    conn: Connection, attempt_id: int, payment_info: dict[str, Any], info_hash: str, capture_deadline: int,
    refund_deadline: int, authorize_tx: str,
) -> int:
    """Book an approved attempt whose money the escrow now holds: status escrowed, an escrow_payments row, and
    Dr escrow / Cr cash, all in one transaction. Deadlines are unix seconds. Returns the journal entry id."""
    with transaction(conn):
        row = _locked(conn, attempt_id, "approved")
        conn.execute("UPDATE payment_attempts SET status = 'escrowed' WHERE id = %s", (attempt_id,))
        conn.execute(
            "INSERT INTO escrow_payments (attempt_id, payment_info, info_hash, capture_deadline, refund_deadline,"
            " authorize_tx) VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s)",
            (attempt_id, json.dumps(payment_info), info_hash, capture_deadline, refund_deadline, authorize_tx),
        )
        return post_entry(
            conn,
            f"x402 escrow hold {row['url']} tx {authorize_tx}",
            [(escrow_code(conn, row), row["amount"], 0), (row["cash_code"], 0, row["amount"])],
            attempt_id=attempt_id, step="hold",
        )


def record_delivery(
    conn: Connection, attempt_id: int, body: bytes | str, content_type: str | None, verdict: str, rule: str,
    reason: str, held: bool,
) -> None:
    """Keep what an escrowed seller delivered and the delivery check's verdict, before the money moves.

    The body is kept whatever the verdict, so the owner can look at a held delivery; the agent only gets it once the
    payment is captured (settled).
    """
    with transaction(conn):
        _locked(conn, attempt_id, "escrowed")
        conn.execute(
            "UPDATE payment_attempts SET response_hash = %s, delivery_verdict = %s, delivery_reason = %s WHERE id = %s",
            (sha256_hex(body), verdict, f"{rule}: {reason}", attempt_id),
        )
        _store_body(conn, attempt_id, body, content_type)
        conn.execute(
            "UPDATE escrow_payments SET verdict = %s, verdict_rule = %s, verdict_reason = %s, held = %s"
            " WHERE attempt_id = %s",
            (verdict, rule, reason, held, attempt_id),
        )


def capture_attempt(conn: Connection, attempt_id: int, capture_tx: str, decided_by: str) -> int:
    """Book a held payment the seller was paid: settled, Dr expense / Cr escrow. Returns the journal entry id."""
    with transaction(conn):
        row = _locked(conn, attempt_id, "escrowed")
        conn.execute(
            "UPDATE payment_attempts SET status = 'settled', tx_hash = %s, settled_at = now() WHERE id = %s",
            (capture_tx, attempt_id),
        )
        _decide(conn, attempt_id, "captured", "capture_tx", capture_tx, decided_by)
        return post_entry(
            conn,
            f"x402 escrow capture {row['url']} tx {capture_tx}",
            [(row["expense_code"], row["amount"], 0), (escrow_code(conn, row), 0, row["amount"])],
            attempt_id=attempt_id, step="capture",
        )


def void_attempt(conn: Connection, attempt_id: int, void_tx: str, decided_by: str) -> int:
    """Book a held payment that went back to the buyer: voided, Dr cash / Cr escrow. Returns the journal entry id."""
    with transaction(conn):
        row = _locked(conn, attempt_id, "escrowed")
        conn.execute("UPDATE payment_attempts SET status = 'voided' WHERE id = %s", (attempt_id,))
        _decide(conn, attempt_id, "voided", "void_tx", void_tx, decided_by)
        return post_entry(
            conn,
            f"x402 escrow void {row['url']} tx {void_tx}",
            [(row["cash_code"], row["amount"], 0), (escrow_code(conn, row), 0, row["amount"])],
            attempt_id=attempt_id, step="void",
        )


def _decide(conn: Connection, attempt_id: int, state: str, tx_column: str, tx: str, decided_by: str) -> None:
    conn.execute(
        f"UPDATE escrow_payments SET state = %s, {tx_column} = %s, held = false, decided_by = %s, decided_at = now()"
        " WHERE attempt_id = %s AND state = 'authorized'",
        (state, tx, decided_by, attempt_id),
    )


def post_funding(conn: Connection, amount: int, memo: str) -> int:
    """Record USDC the owner put into the wallet: Dr USDC Cash / Cr Owner Contributions.

    Needed so the trial balance is complete and cash does not start negative.
    """
    with transaction(conn):
        return post_entry(conn, memo, [(CASH, amount, 0), (OWNER_CONTRIBUTIONS, 0, amount)])


def reverse_entry(conn: Connection, entry_id: int, memo: str) -> int:
    """Correct an entry by posting a new one with debits and credits swapped. Returns the new entry id.

    This is the only way to fix the books: history is never edited.
    """
    with transaction(conn):
        lines = conn.execute(
            f"SELECT a.code, (l.debit {ATOMIC})::bigint AS debit, (l.credit {ATOMIC})::bigint AS credit"
            " FROM journal_lines l JOIN accounts a ON a.id = l.account_id WHERE l.entry_id = %s",
            (entry_id,),
        ).fetchall()
        swapped = [(r["code"], r["credit"], r["debit"]) for r in lines]
        return post_entry(conn, f"Reversal of {entry_id}: {memo}", swapped, reverses=entry_id)


def has_entries(conn: Connection) -> bool:
    """Return True if anything has been posted yet. Used to book the opening balance only once."""
    return conn.execute("SELECT 1 AS hit FROM journal_entries LIMIT 1").fetchone() is not None


def trial_balance(conn: Connection) -> list[dict[str, Any]]:
    """Return code, name, total debit and total credit (atomic units) for every account, ordered by code."""
    return conn.execute(
        f"SELECT a.code, a.name, (COALESCE(SUM(l.debit), 0) {ATOMIC})::bigint AS debit,"
        f" (COALESCE(SUM(l.credit), 0) {ATOMIC})::bigint AS credit"
        " FROM accounts a LEFT JOIN journal_lines l ON l.account_id = a.id GROUP BY a.id ORDER BY a.code"
    ).fetchall()
