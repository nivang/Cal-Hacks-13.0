"""Double entry journal: settlements, funding, reversals and the trial balance.

Every entry is balanced and append only (db.py triggers reject edits and deletes). A settlement
updates the attempt and posts its entry in one transaction, so the books never drift from the
payment log. Used by payments.py after a payment settles.
"""
import hashlib
import sqlite3

from db import account_id, insert, now, transaction

CASH = "1010"
OWNER_CONTRIBUTIONS = "3000"


class UnbalancedEntry(ValueError):
    """Raised when an entry's debits do not equal its credits. Nothing is written."""


def sha256_hex(data: bytes | str) -> str:
    """Return the SHA256 of a response body, so we can prove what was received without storing it."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def post_entry(
    conn: sqlite3.Connection,
    memo: str,
    lines: list[tuple[str, int, int]],
    attempt_id: int | None = None,
    reverses: int | None = None,
) -> int:
    """Write one journal entry and return its id. Call only inside a transaction.

    lines are (account code, debit, credit). Debits must equal credits and be above zero, otherwise
    UnbalancedEntry is raised before anything is written.
    """
    debits = sum(debit for _, debit, _ in lines)
    credits = sum(credit for _, _, credit in lines)
    if debits != credits or debits == 0:
        raise UnbalancedEntry(f"debits {debits} != credits {credits}")
    entry_id = insert(
        conn,
        "INSERT INTO journal_entries (attempt_id, reverses_entry_id, date, memo) VALUES (?, ?, ?, ?)",
        (attempt_id, reverses, now(), memo),
    )
    for code, debit, credit in lines:
        conn.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (?, ?, ?, ?)",
            (entry_id, account_id(conn, code), debit, credit),
        )
    return entry_id


def settle_attempt(conn: sqlite3.Connection, attempt_id: int, tx_hash: str, response_body: bytes | str) -> int:
    """Mark an approved attempt settled and post Dr expense / Cr USDC Cash in one transaction.

    Stores the tx hash and the response hash. Returns the journal entry id. Only approved attempts
    can settle, so a blocked or failed attempt can never reach the books.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT a.url, a.amount, a.status, ac.code AS expense_code FROM payment_attempts a"
            " JOIN agents ag ON ag.id = a.agent_id JOIN accounts ac ON ac.id = ag.expense_account_id"
            " WHERE a.id = ?",
            (attempt_id,),
        ).fetchone()
        if row["status"] != "approved":
            raise ValueError(f"attempt {attempt_id} is {row['status']}, not approved")
        conn.execute(
            "UPDATE payment_attempts SET status = 'settled', tx_hash = ?, response_hash = ?, settled_at = ?"
            " WHERE id = ?",
            (tx_hash, sha256_hex(response_body), now(), attempt_id),
        )
        return post_entry(
            conn,
            f"x402 payment {row['url']} tx {tx_hash}",
            [(row["expense_code"], row["amount"], 0), (CASH, 0, row["amount"])],
            attempt_id=attempt_id,
        )


def post_funding(conn: sqlite3.Connection, amount: int, memo: str) -> int:
    """Record USDC the owner put into the wallet: Dr USDC Cash / Cr Owner Contributions.

    Needed so the trial balance is complete and cash does not start negative.
    """
    with transaction(conn):
        return post_entry(conn, memo, [(CASH, amount, 0), (OWNER_CONTRIBUTIONS, 0, amount)])


def reverse_entry(conn: sqlite3.Connection, entry_id: int, memo: str) -> int:
    """Correct an entry by posting a new one with debits and credits swapped. Returns the new entry id.

    This is the only way to fix the books: history is never edited.
    """
    with transaction(conn):
        lines = conn.execute(
            "SELECT a.code, l.debit, l.credit FROM journal_lines l JOIN accounts a ON a.id = l.account_id"
            " WHERE l.entry_id = ?",
            (entry_id,),
        ).fetchall()
        swapped = [(r["code"], r["credit"], r["debit"]) for r in lines]
        return post_entry(conn, f"Reversal of {entry_id}: {memo}", swapped, reverses=entry_id)


def has_entries(conn: sqlite3.Connection) -> bool:
    """Return True if anything has been posted yet. Used to book the opening balance only once."""
    return conn.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone() is not None


def trial_balance(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return code, name, total debit and total credit for every account, ordered by code."""
    return conn.execute(
        "SELECT a.code, a.name, COALESCE(SUM(l.debit), 0) AS debit, COALESCE(SUM(l.credit), 0) AS credit"
        " FROM accounts a LEFT JOIN journal_lines l ON l.account_id = a.id GROUP BY a.id ORDER BY a.code"
    ).fetchall()
