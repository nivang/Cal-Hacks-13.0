"""Tests for agent tokens: issuing, hashing, lookup, rotation and the env file writer."""
import pytest

import auth


def test_issue_then_lookup_round_trips(conn):
    token = auth.issue_token(conn, "research-agent")
    assert token.startswith("snt_") and len(token) > 40
    assert auth.agent_for_token(conn, token) == "research-agent"


def test_only_the_hash_is_stored(conn):
    token = auth.issue_token(conn, "research-agent")
    stored = conn.execute("SELECT token_hash FROM agents WHERE name = 'research-agent'").fetchone()["token_hash"]
    assert stored == auth.hash_token(token) and token not in stored


def test_wrong_or_malformed_tokens_find_nobody(conn):
    auth.issue_token(conn, "research-agent")
    assert auth.agent_for_token(conn, "snt_" + "x" * 43) is None
    assert auth.agent_for_token(conn, "not-a-token") is None
    assert auth.agent_for_token(conn, "") is None


def test_rotating_a_token_invalidates_the_old_one(conn):
    old = auth.issue_token(conn, "research-agent")
    new = auth.issue_token(conn, "research-agent")
    assert old != new
    assert auth.agent_for_token(conn, old) is None
    assert auth.agent_for_token(conn, new) == "research-agent"


def test_unknown_agent_cannot_get_a_token(conn):
    with pytest.raises(LookupError):
        auth.issue_token(conn, "nobody")


def test_two_agents_cannot_share_a_token_hash(conn):
    conn.execute(
        "INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id)"
        " SELECT 'second-agent', 1, 1, expense_account_id FROM agents WHERE name = 'research-agent'"
    )
    token = auth.issue_token(conn, "research-agent")
    from db import UniqueViolation, transaction

    with pytest.raises(UniqueViolation), transaction(conn):
        conn.execute("UPDATE agents SET token_hash = %s WHERE name = 'second-agent'", (auth.hash_token(token),))


def test_write_env_var_replaces_or_appends_without_touching_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nSENTINEL_AGENT_TOKEN=old\nB=2\n")
    auth.write_env_var(env, "SENTINEL_AGENT_TOKEN", "new")
    assert env.read_text() == "A=1\nB=2\nSENTINEL_AGENT_TOKEN=new\n"
    auth.write_env_var(tmp_path / "fresh.env", "X", "y")
    assert (tmp_path / "fresh.env").read_text() == "X=y\n"
