"""Postgres (Supabase) connection pool, tracked migrations and seed data for Sentinel's store.

The tables live in Supabase Postgres (see migrations/*.sql): agents, sellers (the operator's list plus each
user's own), payment_attempts, approvals, accounts, journal_entries, journal_lines and attempt_results, plus users,
magic_links and the oauth_* tables for sign in.
attempts.py and ledger.py read and write them through the connections this module hands out. Every other file
imports from here and never touches the driver, so swapping databases again means editing this file and the SQL in
the modules that query it.

Migrations are tracked: schema_migrations records each migrations/*.sql file once it has run, and apply_migrations
runs only the files not recorded yet. From now on they go through `python -m sentinel.db migrate` only, never by
hand or another tool, so the record stays true. A database built before tracking began (it has the tables but no
records) is checked to be at 006 and recorded as that baseline the first time; 007 runs again (it only changes
rows). pending_migrations lets a server refuse to start on a database that is behind.

Money: the database stores USDC as numeric(18,6) (0.500000). The rest of the code works in integer atomic units
(500000) and converts here with usdc() and in the SQL of the modules that read amounts back. Never floats.
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
DEFAULT_PER_PAYMENT_CAP = 10 * USDC  # limits every new agent starts with; the owner changes them on the ledger page
DEFAULT_WEEKLY_BUDGET = 50 * USDC
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 0x53454E54494E454C  # advisory lock key ("SENTINEL" in ASCII): one migration run at a time
BASELINE = "006"  # the last migration a pre tracking database provably has; 007 only changes rows, so it runs again

CHART_OF_ACCOUNTS = [
    ("1010", "USDC Cash", "asset"),
    ("1050", "USDC in Escrow", "asset"),
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
            max_size=10,
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


def migration_files() -> list[Path]:
    """Every migrations/*.sql file, in the order they run (by name, which starts with the number)."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _in_baseline(name: str) -> bool:
    """True for the files a database built before tracking already has: numbers 001 to BASELINE."""
    return name.split("_", 1)[0] <= BASELINE


def _exists(conn: Connection, table: str) -> bool:
    """True if the table exists on the current search_path (the "test" schema in the test suite)."""
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS found", (table,)).fetchone()
    return bool(row and row["found"])


def _recorded(conn: Connection) -> set[str]:
    """The migration file names schema_migrations says have run."""
    return {row["name"] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}


def apply_migrations(conn: Connection) -> list[str]:
    """Run every migrations/*.sql file not yet recorded in schema_migrations, in order, and return their names.

    It is all one transaction (a savepoint when the caller already has one open, as the test suite does) under an
    advisory lock, so two migrators never run a file twice and a file that fails leaves nothing behind. A database
    built before tracking began has payment_attempts but no records: its files 001 to 006 are recorded as the
    baseline without running, after checking it really reached 006 (attempt_results exists). 007 only changes rows,
    so table checks cannot show it ran; it is safe to repeat, so it simply runs. The SQL has no schema prefix and
    everything resolves through search_path, which is how the test suite builds the "test" schema from the same files.
    """
    applied: list[str] = []
    with transaction(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute("ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY")  # default deny, as in 004
        conn.execute("REVOKE ALL ON schema_migrations FROM anon, authenticated")
        done = _recorded(conn)
        if not done and _exists(conn, "payment_attempts"):
            if not _exists(conn, "attempt_results"):
                raise RuntimeError(
                    "This database predates migration 006 (it has payment_attempts but no attempt_results). "
                    "Bring it to 006 by hand, then run python -m sentinel.db migrate again."
                )
            for path in migration_files():
                if _in_baseline(path.name):
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                    done.add(path.name)
        for path in migration_files():
            if path.name not in done:
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
    return applied


def pending_migrations(conn: Connection) -> list[str]:
    """Return the migration file names apply_migrations would run now, in order, without changing anything.

    For startup checks: a server should refuse to run while this is not empty. A database that predates 006 still
    lists files here, so it is refused too; apply_migrations then says what to do.
    """
    names = [path.name for path in migration_files()]
    done = _recorded(conn) if _exists(conn, "schema_migrations") else set()
    if not done and _exists(conn, "payment_attempts"):
        done = {name for name in names if _in_baseline(name)}
    return [name for name in names if name not in done]


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
        ("research-agent", usdc(DEFAULT_PER_PAYMENT_CAP), usdc(DEFAULT_WEEKLY_BUDGET), account_id(conn, "6100")),
    )


def seed_sellers(conn: Connection, sellers: list[dict[str, str]]) -> None:
    """Add sellers to the operator's list (agent_id null; or whichever status is given), skipping ones already there.

    Each dict needs name, pay_to, network, asset and status. These come from the human's config,
    never from text the agent read online.
    """
    for s in sellers:
        conn.execute(
            "INSERT INTO sellers (name, pay_to, network, asset, status, added_by) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (lower(pay_to), network, lower(asset)) WHERE agent_id IS NULL DO NOTHING",
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
        sys.exit("usage: python -m sentinel.db migrate (runs the migrations this database does not have yet)")
    with connection() as c:
        applied = apply_migrations(c)
        init(c)
    close_pool()
    print(f"applied {', '.join(applied)}" if applied else "no pending migrations; the database is up to date")
