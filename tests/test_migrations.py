"""Tests for the tracked migration runner (db.apply_migrations, db.pending_migrations) and the schema 008 adds.

Each runner test builds a throwaway schema inside the rolled back test transaction (the conn fixture), so nothing
here is ever committed and the shared "test" schema is only read. Schema names are random, so two test runs at once
never wait on each other's uncommitted schema. Covers a fresh database, one built before tracking began (recorded
as the 006 baseline, with 007 run again since only rows show it), the refusal for a database older than 006, and
that the "test" schema is up to date.
"""
import uuid

import pytest

from sentinel import db

ALL = [path.name for path in db.migration_files()]
BASELINE = [name for name in ALL if name.split("_", 1)[0] <= "006"]
AFTER_BASELINE = ALL[len(BASELINE):]
NEW_TABLES = ["users", "magic_links", "oauth_clients", "oauth_codes", "oauth_tokens"]


def scratch(conn):
    """Create an empty schema in this test's transaction and point the connection at it. Rolled back with the test."""
    name = f"scratch_{uuid.uuid4().hex[:12]}"
    conn.execute(f"CREATE SCHEMA {name}")
    db.use_schema(conn, name)
    return name


def recorded(conn):
    return [row["name"] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()]


def exists(conn, table):
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS found", (table,)).fetchone()["found"]


def refused(conn, statement, params=None):
    """Assert the database rejects a statement, inside its own savepoint so the test transaction stays usable."""
    with pytest.raises(db.IntegrityError), db.transaction(conn):
        conn.execute(statement, params)


def test_a_fresh_schema_gets_every_migration_in_order_and_a_second_run_does_nothing(conn):
    scratch(conn)
    applied = db.apply_migrations(conn)
    assert applied == ALL and [name[:3] for name in applied[:8]] == [f"00{i}" for i in range(1, 9)]
    assert recorded(conn) == ALL
    assert all(exists(conn, table) for table in NEW_TABLES)
    assert db.apply_migrations(conn) == []
    assert db.pending_migrations(conn) == []


def test_pending_on_an_empty_schema_lists_every_file_and_changes_nothing(conn):
    scratch(conn)
    assert db.pending_migrations(conn) == ALL
    assert not exists(conn, "schema_migrations")


def test_a_database_built_before_tracking_is_recorded_as_the_006_baseline_and_007_runs_again(conn):
    scratch(conn)
    for path in db.migration_files():
        if path.name in BASELINE:
            conn.execute(path.read_text())
    assert BASELINE[-1].startswith("006") and AFTER_BASELINE[0].startswith("007")
    db.init(conn)  # the chart of accounts and research-agent, as an old database has them
    conn.execute(  # a pending row left by an old run: it holds the in flight lock until 007 closes it
        "INSERT INTO payment_attempts (agent_id, url, dedupe_key, status)"
        " VALUES ((SELECT id FROM agents WHERE name = 'research-agent'), 'http://seller.test/x', 'key', 'pending')"
    )
    assert db.pending_migrations(conn) == AFTER_BASELINE
    assert db.apply_migrations(conn) == AFTER_BASELINE  # 001 to 006 are not run again (their tables exist)
    assert recorded(conn) == ALL
    statuses = [row["status"] for row in conn.execute("SELECT status FROM payment_attempts").fetchall()]
    assert statuses == ["blocked"]


def test_a_database_that_predates_006_is_refused_and_nothing_is_recorded(conn):
    scratch(conn)
    conn.execute(db.migration_files()[0].read_text())  # 001 only: payment_attempts but no attempt_results
    with pytest.raises(RuntimeError, match="predates migration 006"):
        db.apply_migrations(conn)
    assert not exists(conn, "schema_migrations")
    assert db.pending_migrations(conn) != []  # so a server still refuses to start on it


def test_the_test_schema_is_up_to_date_after_the_session_fixture(conn):
    assert db.pending_migrations(conn) == []
    assert recorded(conn) == ALL


def test_the_new_tables_are_default_deny_and_viewers_may_read_only_the_cash_account_column(conn):
    tables = [*NEW_TABLES, "schema_migrations"]
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = current_schema() AND c.relname = ANY(%s)",
        (tables,),
    ).fetchall()
    assert {row["relname"]: row["relrowsecurity"] for row in rows} == dict.fromkeys(tables, True)
    for role in ("anon", "authenticated"):
        for table in tables:
            granted = conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE') AS p", (role, table)
            ).fetchone()
            assert not granted["p"], f"{role} has access to {table}"
        for sequence in ("magic_links_id_seq", "oauth_codes_id_seq", "oauth_tokens_id_seq"):
            granted = conn.execute(
                "SELECT has_sequence_privilege(%s, %s, 'USAGE, SELECT, UPDATE') AS p", (role, sequence)
            ).fetchone()
            assert not granted["p"], f"{role} has access to {sequence}"
    columns = conn.execute(
        "SELECT has_column_privilege('authenticated', 'agents', 'cash_account_id', 'SELECT') AS cash,"
        " has_column_privilege('authenticated', 'agents', 'token_hash', 'SELECT') AS token"
    ).fetchone()
    assert (columns["cash"], columns["token"]) == (True, False)


GRANT = (
    "INSERT INTO oauth_codes (code_hash, request_nonce, client_id, user_id, redirect_uri,"
    " redirect_uri_provided_explicitly, code_challenge, scopes, resource, expires_at, grant_expires_at)"
    " VALUES ('code', 'nonce', 'client', %s, 'http://127.0.0.1/callback', true, 'challenge', '{pay}',"
    " 'http://localhost:8000/mcp', now() + interval '5 minutes', now() + interval '90 days') RETURNING id"
)
LINK = (
    "INSERT INTO magic_links (token_hash, email, email_key, limit_key, ip_hash, next_path, expires_at)"
    " VALUES (%s, 'a@example.com', 'a@example.com', 'a@example.com', 'ip', %s, now() + make_interval(mins => %s))"
)


def test_008_keeps_the_check_constraints(conn):
    user_id = conn.execute(
        "INSERT INTO users (email, email_key) VALUES ('Ann@Example.com', 'ann@example.com') RETURNING id"
    ).fetchone()["id"]
    refused(conn, "INSERT INTO users (email, email_key) VALUES ('Bo@Example.com', 'Bo@Example.com')")  # key lowercase
    refused(conn, "INSERT INTO users (email, email_key) VALUES (repeat('a', 250) || '@x.io', 'long@x.io')")  # > 254
    refused(conn, "UPDATE users SET agent_id = (SELECT min(id) FROM agents)")  # an agent needs a wallet too
    refused(conn, "UPDATE users SET wallet_address = '0x' || repeat('a', 40)")  # and a wallet needs an agent
    refused(conn, "UPDATE users SET wallet_address = '0x12', agent_id = (SELECT min(id) FROM agents)")  # not hex 40

    conn.execute(LINK, ("link-ok", "/oauth/consent?req=x", 15))
    refused(conn, LINK, ("link-protocol-relative", "//evil.example/path", 15))
    refused(conn, LINK, ("link-absolute", "https://evil.example/", 15))
    refused(conn, LINK, ("link-expired-at-birth", None, 0))

    client = "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
    conn.execute(client + " VALUES ('client', '{}', 'none', 'ip')")
    refused(conn, client + " VALUES ('jwt', '{}', 'private_key_jwt', 'ip')")
    refused(conn, client + " VALUES ('big', jsonb_build_object('name', repeat('a', 5000)), 'none', 'ip')")

    grant_id = conn.execute(GRANT, (user_id,)).fetchone()["id"]
    token = "INSERT INTO oauth_tokens (token_hash, kind, grant_id, expires_at, used_at) VALUES (%s, %s, %s, now(), %s)"
    conn.execute(token, ("refresh-rotated", "refresh", grant_id, "2026-01-01"))
    refused(conn, token, ("access-used", "access", grant_id, "2026-01-01"))  # only refresh tokens are rotated
    refused(conn, token, ("id-token", "id", grant_id, None))
