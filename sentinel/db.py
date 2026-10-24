"""Postgres (Supabase) connection pool, migrations and seed data for Sentinel's store.

The tables live in Supabase Postgres (see migrations/*.sql): agents, sellers, payment_attempts,
approvals, accounts, journal_entries, journal_lines. attempts.py and ledger.py read and write them
through the connections this module hands out. Every other file imports from here and never touches
the driver, so swapping databases again means editing this file and the SQL in those two modules.

Money: the database stores USDC as numeric(18,6) (0.500000). The rest of the code works in integer
atomic units (500000) and converts here with usdc() and atomic(). Never floats.
"""
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Connection = psycopg.Connection[dict[str, Any]]
IntegrityError = psycopg.IntegrityError  # constraint and trigger rejections (append only, unbalanced entry)
DatabaseError = psycopg.DatabaseError

__all__ = ["Connection", "DatabaseError", "IntegrityError", "UniqueViolation"]

ATOMIC_PER_USDC = 1_000_000
USDC = ATOMIC_PER_USDC  # one USDC in atomic units
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

CHART_OF_ACCOUNTS = [
    ("1010", "USDC Cash", "asset"),
    ("3000", "Owner Contributions", "equity"),
    ("6100", "Research Data Expense", "expense"),
    ("6200", "Software and Tools Expense", "expense"),
]

_pool: ConnectionPool | None = None


def usdc(atomic_units: int) -> Decimal:
    """Convert integer atomic units (500000) to the whole USDC Decimal the database stores (0.500000)."""
    return Decimal(atomic_units) / ATOMIC_PER_USDC


def get_pool() -> ConnectionPool:
    """Return the shared connection pool, creating it from DATABASE_URL on first use.

    Autocommit is on so that transaction() decides exactly when a group of writes commits.
    prepare_threshold=None is required by Supabase's pooled connection string (pgbouncer style
    pooling does not support server side prepared statements).
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            check=ConnectionPool.check_connection,  # drop connections the server closed while idle
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Close the shared pool. Call at the end of a script so it exits cleanly."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[Connection]:
    """Borrow a pooled connection for the duration of a with block, then return it to the pool."""
    with get_pool().connection() as conn:
        yield cast(Connection, conn)


def transaction(conn: Connection, rollback: bool = False) -> Any:
    """Context manager that runs a block as one transaction: commit if it finishes, roll back if it raises.

    Nested uses become savepoints. Used so a settlement and its journal entry are written together
    or not at all, and so an expected constraint failure does not poison an outer transaction.
    rollback=True always rolls back at the end, which the test suite uses to keep tests isolated.
    """
    return conn.transaction(force_rollback=rollback)


def use_schema(conn: Connection, schema: str) -> None:
    """Point the current transaction at another schema (used by the test suite for "test").

    Must run inside a transaction; SET LOCAL ends with it, so it is safe behind a pooled connection.
    """
    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))


def insert(conn: Connection, query: str, params: tuple[Any, ...]) -> int:
    """Run an INSERT (without RETURNING) and return the new row's id."""
    row = conn.execute(query + " RETURNING id", params).fetchone()
    if row is None:
        raise RuntimeError("insert returned no row id")
    return int(row["id"])


def apply_migrations(conn: Connection) -> None:
    """Run every migrations/*.sql file in order if the tables do not exist yet.

    The Supabase project already has these applied through the Supabase migrations tool. This is for
    a fresh database, and for building the "test" schema, which is why the SQL has no schema prefix.
    """
    if conn.execute("SELECT to_regclass('payment_attempts') AS t").fetchone()["t"] is not None:  # type: ignore[index]
        return
    with transaction(conn):
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(path.read_text())


def init(conn: Connection) -> None:
    """Seed the chart of accounts and the default agent. Safe to call on every start (ON CONFLICT DO NOTHING)."""
    for code, name, type_ in CHART_OF_ACCOUNTS:
        conn.execute(
            "INSERT INTO accounts (code, name, type) VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING",
            (code, name, type_),
        )
    conn.execute(
        "INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
        ("research-agent", usdc(10 * USDC), usdc(50 * USDC), account_id(conn, "6100")),
    )


def seed_sellers(conn: Connection, sellers: list[dict[str, str]]) -> None:
    """Add sellers to the approved list (or whichever status is given), skipping ones already present.

    Each dict needs name, pay_to, network, asset and status. These come from the human's config,
    never from text the agent read online.
    """
    for s in sellers:
        conn.execute(
            "INSERT INTO sellers (name, pay_to, network, asset, status, added_by) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (pay_to, network, asset) DO NOTHING",
            (s["name"], s["pay_to"], s["network"], s["asset"], s["status"], "human"),
        )


def account_id(conn: Connection, code: str) -> int:
    """Return the id of the account with this chart of accounts code, e.g. "1010"."""
    row = conn.execute("SELECT id FROM accounts WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise LookupError(f"no account with code {code}")
    return int(row["id"])


def agent_id(conn: Connection, name: str) -> int:
    """Return the id of the agent with this name."""
    row = conn.execute("SELECT id FROM agents WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"no agent named {name}")
    return int(row["id"])


if __name__ == "__main__":
    from dotenv import load_dotenv

    from sentinel import ENV_FILE

    load_dotenv(ENV_FILE)
    if sys.argv[1:] != ["migrate"]:
        sys.exit("usage: python -m sentinel.db migrate (builds the tables on an empty database)")
    with connection() as c:
        apply_migrations(c)
        init(c)
    close_pool()
    print("migrations applied")
