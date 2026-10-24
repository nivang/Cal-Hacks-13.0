"""Double entry journal: settlements, funding, reversals and the trial balance.

Every entry is balanced and append only. Both rules are enforced by the database (triggers in
migrations/002 and 003), and balance is also checked here first for a clear error. A settlement
updates the attempt and posts its entry in one transaction, so the books never drift from the
payment log. Used by payments.py after a payment settles. Amounts are integer atomic units.
"""
import hashlib
from typing import Any

from db import Connection, account_id, insert, transaction, usdc

CASH = "1010"
OWNER_CONTRIBUTIONS = "3000"
ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units


class UnbalancedEntry(ValueError):
    """Raised when an entry's debits do not equal its credits. Nothing is written."""


def sha256_hex(data: bytes | str) -> str:
    """Return the SHA256 of a response body, so we can prove what was received without storing it."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def post_entry(
    conn: Connection,
    memo: str,
    lines: list[tuple[str, int, int]],
    attempt_id: int | None = None,
    reverses: int | None = None,
) -> int:
    """Write one journal entry and return its id. Call only inside a transaction.

    lines are (account code, debit, credit) in atomic units. Debits must equal credits and be above
    zero, otherwise UnbalancedEntry is raised before anything is written.
    """
    debits = sum(debit for _, debit, _ in lines)
    credits = sum(credit for _, _, credit in lines)
    if debits != credits or debits == 0:
        raise UnbalancedEntry(f"debits {debits} != credits {credits}")
    entry_id = insert(
        conn,
        "INSERT INTO journal_entries (attempt_id, reverses_entry_id, memo) VALUES (%s, %s, %s)",
        (attempt_id, reverses, memo),
    )
    for code, debit, credit in lines:
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, %s, %s)",
            (entry_id, account_id(conn, code), usdc(debit), usdc(credit)),
        )
    return entry_id


def settle_attempt(conn: Connection, attempt_id: int, tx_hash: str, response_body: bytes | str) -> int:
    """Mark an approved attempt settled and post Dr expense / Cr USDC Cash in one transaction.

    Stores the tx hash and the response hash. Returns the journal entry id. Only approved attempts
    can settle, so a blocked or failed attempt can never reach the books. The row is locked while we
    work so two settlers cannot both book the same payment.
    """
    with transaction(conn):
        row = conn.execute(
            f"SELECT a.url, (a.amount {ATOMIC})::bigint AS amount, a.status, ac.code AS expense_code"
            " FROM payment_attempts a JOIN agents ag ON ag.id = a.agent_id"
            " JOIN accounts ac ON ac.id = ag.expense_account_id WHERE a.id = %s FOR UPDATE OF a",
            (attempt_id,),
        ).fetchone()
        if row is None or row["status"] != "approved":
            status = row["status"] if row else "missing"
            raise ValueError(f"attempt {attempt_id} is {status}, not approved")
        conn.execute(
            "UPDATE payment_attempts SET status = 'settled', tx_hash = %s, response_hash = %s, settled_at = now()"
            " WHERE id = %s",
            (tx_hash, sha256_hex(response_body), attempt_id),
        )
        return post_entry(
            conn,
            f"x402 payment {row['url']} tx {tx_hash}",
            [(row["expense_code"], row["amount"], 0), (CASH, 0, row["amount"])],
            attempt_id=attempt_id,
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
