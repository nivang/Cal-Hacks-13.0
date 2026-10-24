"""SQLite connection, schema and seed data for Sentinel's store.

Holds the seven tables (agents, sellers, payment_attempts, approvals, accounts, journal_entries,
journal_lines). attempts.py and ledger.py read and write them. Amounts are integers in USDC atomic
units (6 decimals), never floats.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get("SENTINEL_DB", "sentinel.db")
USDC = 1_000_000  # one USDC in atomic units

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('asset', 'expense', 'equity'))
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT,
    per_payment_cap INTEGER NOT NULL,
    weekly_budget INTEGER NOT NULL,
    expense_account_id INTEGER NOT NULL REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS sellers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pay_to TEXT NOT NULL,
    network TEXT NOT NULL,
    asset TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('approved', 'pending', 'blocked')),
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE (pay_to, network, asset)
);
CREATE TABLE IF NOT EXISTS payment_attempts (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    url TEXT NOT NULL,
    reason TEXT,
    pay_to TEXT,
    amount INTEGER,
    quote_json TEXT,
    dedupe_key TEXT,
    decision TEXT,
    rule_fired TEXT,
    status TEXT NOT NULL CHECK (status IN ('blocked', 'pending', 'approved', 'denied', 'settled', 'failed')),
    tx_hash TEXT,
    response_hash TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
-- In flight lock: one live attempt per key, so two simultaneous identical calls cannot both pass.
CREATE UNIQUE INDEX IF NOT EXISTS one_inflight_per_key ON payment_attempts(dedupe_key)
    WHERE status IN ('pending', 'approved');
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES payment_attempts(id),
    approver TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
    note TEXT,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER REFERENCES payment_attempts(id),
    reverses_entry_id INTEGER REFERENCES journal_entries(id),
    date TEXT NOT NULL,
    memo TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_lines (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    debit INTEGER NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit INTEGER NOT NULL DEFAULT 0 CHECK (credit >= 0),
    CHECK ((debit = 0) != (credit = 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_entry_per_attempt
    ON journal_entries(attempt_id) WHERE attempt_id IS NOT NULL AND reverses_entry_id IS NULL;

-- Append only: the books and approvals can be added to, never edited or deleted.
-- Corrections are reversing entries (ledger.reverse_entry).
CREATE TRIGGER IF NOT EXISTS journal_entries_no_update BEFORE UPDATE ON journal_entries
    BEGIN SELECT RAISE(ABORT, 'journal is append only'); END;
CREATE TRIGGER IF NOT EXISTS journal_entries_no_delete BEFORE DELETE ON journal_entries
    BEGIN SELECT RAISE(ABORT, 'journal is append only'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_no_update BEFORE UPDATE ON journal_lines
    BEGIN SELECT RAISE(ABORT, 'journal is append only'); END;
CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
    BEGIN SELECT RAISE(ABORT, 'journal is append only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals are append only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals are append only'); END;
"""

CHART_OF_ACCOUNTS = [
    ("1010", "USDC Cash", "asset"),
    ("3000", "Owner Contributions", "equity"),
    ("6100", "Research Data Expense", "expense"),
    ("6200", "Software and Tools Expense", "expense"),
]


def now() -> str:
    """Return the current UTC time as an ISO 8601 string. Every timestamp in the store uses this."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database with rows as dicts and foreign keys on.

    Autocommit is used so that transaction() controls exactly when a write group commits.
    """
    conn = sqlite3.connect(path or DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one transaction: commit if it finishes, roll back if it raises.

    Used so a settlement and its journal entry are written together or not at all.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def insert(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    """Run an INSERT and return the new row id."""
    row_id = conn.execute(sql, params).lastrowid
    if row_id is None:
        raise RuntimeError("insert returned no row id")
    return row_id


def init(conn: sqlite3.Connection) -> None:
    """Create the tables if missing and seed the chart of accounts and the default agent.

    Safe to call on every start: all seed rows use INSERT OR IGNORE.
    """
    conn.executescript(SCHEMA)
    for code, name, type_ in CHART_OF_ACCOUNTS:
        conn.execute("INSERT OR IGNORE INTO accounts (code, name, type) VALUES (?, ?, ?)", (code, name, type_))
    conn.execute(
        "INSERT OR IGNORE INTO agents (name, per_payment_cap, weekly_budget, expense_account_id) VALUES (?, ?, ?, ?)",
        ("research-agent", 10 * USDC, 50 * USDC, account_id(conn, "6100")),
    )


def seed_sellers(conn: sqlite3.Connection, sellers: list[dict[str, str]]) -> None:
    """Add sellers to the approved list (or whichever status is given), skipping ones already present.

    Each dict needs name, pay_to, network, asset and status. These come from the human's config,
    never from text the agent read online.
    """
    for s in sellers:
        conn.execute(
            "INSERT OR IGNORE INTO sellers (name, pay_to, network, asset, status, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (s["name"], s["pay_to"], s["network"], s["asset"], s["status"], "human", now()),
        )


def account_id(conn: sqlite3.Connection, code: str) -> int:
    """Return the id of the account with this chart of accounts code, e.g. "1010"."""
    row = conn.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()
    return int(row["id"])


def agent_id(conn: sqlite3.Connection, name: str) -> int:
    """Return the id of the agent with this name."""
    row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
    return int(row["id"])
