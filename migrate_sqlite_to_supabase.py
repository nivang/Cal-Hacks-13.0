"""One off: copy sentinel.db (SQLite) into the Supabase Postgres database, then verify the copy.

    python migrate_sqlite_to_supabase.py [path/to/sentinel.db]

Rows are copied in dependency order with their original ids and timestamps. It is idempotent: rows
that already exist (same id) are skipped, so it is safe to run twice. SQLite amounts are integer
atomic units and become numeric(18,6) USDC. Read only on the SQLite side; nothing is deleted.
"""
import sqlite3
import sys
from typing import Any

from dotenv import load_dotenv

import db
import ledger

# (table, columns, SQL converters per column). Order matters: parents before children.
USDC_COLUMNS = {"per_payment_cap", "weekly_budget", "amount", "debit", "credit"}
JSON_COLUMNS = {"quote_json"}
TIME_COLUMNS = {"added_at", "created_at", "settled_at", "decided_at", "date"}
TABLES: list[tuple[str, list[str]]] = [
    ("accounts", ["id", "code", "name", "type"]),
    ("agents", ["id", "name", "token_hash", "per_payment_cap", "weekly_budget", "expense_account_id"]),
    ("sellers", ["id", "name", "pay_to", "network", "asset", "status", "added_by", "added_at"]),
    ("payment_attempts", ["id", "agent_id", "url", "reason", "pay_to", "amount", "quote_json", "dedupe_key",
                          "decision", "rule_fired", "status", "tx_hash", "response_hash", "created_at", "settled_at"]),
    ("approvals", ["id", "attempt_id", "approver", "decision", "note", "decided_at"]),
    ("journal_entries", ["id", "attempt_id", "reverses_entry_id", "date", "memo"]),
    ("journal_lines", ["id", "entry_id", "account_id", "debit", "credit"]),
]


def convert(column: str, value: Any) -> Any:
    """Turn one SQLite value into what the Postgres column expects (atomic units to USDC, text to jsonb)."""
    if value is None:
        return None
    if column in USDC_COLUMNS:
        return db.usdc(int(value))
    return value


def placeholder(column: str) -> str:
    """Return the SQL placeholder for a column, adding the cast Postgres needs for text sourced types."""
    if column in JSON_COLUMNS:
        return "%s::jsonb"
    if column in TIME_COLUMNS:
        return "%s::timestamptz"
    return "%s"


def copy_table(src: sqlite3.Connection, dst: db.Connection, table: str, columns: list[str]) -> int:
    """Copy one table, keeping ids. Returns the number of rows read from SQLite."""
    rows = src.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id").fetchall()
    query = (
        f"INSERT INTO {table} ({', '.join(columns)}) OVERRIDING SYSTEM VALUE"
        f" VALUES ({', '.join(placeholder(c) for c in columns)}) ON CONFLICT (id) DO NOTHING"
    )
    for row in rows:
        dst.execute(query, tuple(convert(c, row[c]) for c in columns))
    return len(rows)


def reset_sequences(dst: db.Connection) -> None:
    """Move each identity sequence past the copied ids so new rows do not collide with them."""
    for table, _ in TABLES:
        dst.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1),"
            f" (SELECT MAX(id) FROM {table}) IS NOT NULL)"
        )


def sqlite_trial_balance(src: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Account code -> (total debit, total credit) as SQLite computed it, in atomic units."""
    rows = src.execute(
        "SELECT a.code, COALESCE(SUM(l.debit), 0) AS debit, COALESCE(SUM(l.credit), 0) AS credit"
        " FROM accounts a LEFT JOIN journal_lines l ON l.account_id = a.id GROUP BY a.id ORDER BY a.code"
    ).fetchall()
    return {r["code"]: (r["debit"], r["credit"]) for r in rows}


def verify(src: sqlite3.Connection, dst: db.Connection) -> bool:
    """Compare row counts, entry balance and account balances. Prints the comparison and returns True if all match."""
    ok = True
    print(f"{'table':<18}{'sqlite':>8}{'postgres':>10}")
    for table, _ in TABLES:
        a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        b = dst.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # type: ignore[index]
        ok &= a == b
        print(f"{table:<18}{a:>8}{b:>10}  {'ok' if a == b else 'MISMATCH'}")

    unbalanced = dst.execute(
        "SELECT entry_id FROM journal_lines GROUP BY entry_id HAVING SUM(debit) <> SUM(credit)"
    ).fetchall()
    ok &= not unbalanced
    print(f"\nunbalanced journal entries in postgres: {len(unbalanced)}")

    expected = sqlite_trial_balance(src)
    actual = {r["code"]: (r["debit"], r["credit"]) for r in ledger.trial_balance(dst)}
    print(f"\n{'account':<8}{'sqlite Dr':>12}{'pg Dr':>12}{'sqlite Cr':>12}{'pg Cr':>12}")
    for code in sorted(expected):
        same = expected[code] == actual.get(code)
        ok &= same
        print(f"{code:<8}{expected[code][0]:>12}{actual[code][0]:>12}{expected[code][1]:>12}{actual[code][1]:>12}"
              f"  {'ok' if same else 'MISMATCH'}")
    return ok


def main(sqlite_path: str) -> None:
    """Copy everything in one transaction (so the deferred balance check covers the whole copy), then verify."""
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    with db.connection() as dst:
        with db.transaction(dst):
            for table, columns in TABLES:
                print(f"copied {table}: {copy_table(src, dst, table, columns)} rows read")
            reset_sequences(dst)
        print()
        passed = verify(src, dst)
    db.close_pool()
    sys.exit(0 if passed else "VERIFICATION FAILED")


if __name__ == "__main__":
    load_dotenv()
    main(sys.argv[1] if len(sys.argv) > 1 else "sentinel.db")
