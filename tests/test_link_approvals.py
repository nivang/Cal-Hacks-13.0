"""Real database checks for scoped, expiring, single-use purchase approval."""
from dataclasses import asdict, replace

import pytest

from sentinel import attempts, db, guard, link_approvals, payments

AGENT = "research-agent"
UNKNOWN = "0x" + "a" * 40
QUOTE = guard.Quote("http://seller.test/new", UNKNOWN, 500_000,
                    "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "eip155:84532", 300)
REASON = "Discover email benchmarks for the Q3 report."


def pending(conn, quote=QUOTE):
    with pytest.raises(payments.ApprovalRequired) as caught:
        payments.authorize(conn, AGENT, REASON, quote, interactive=True)
    return caught.value.attempt_id


def accept(conn, request_id):
    return link_approvals.decide(conn, AGENT, request_id, "human", "yes")


def test_unknown_seller_waits_without_reserving_budget(conn):
    request_id = pending(conn)
    row = link_approvals.get(conn, AGENT, request_id)
    assert row["status"] == "pending" and row["approval_expires_at"] is not None
    assert attempts.week_spend(conn, db.agent_id(conn, AGENT), 7) == 0
    assert conn.execute("SELECT count(*) AS n FROM journal_entries").fetchone()["n"] == 0
    assert accept(conn, request_id)["status"] == "authorized"
    assert payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id) == request_id
    assert attempts.week_spend(conn, db.agent_id(conn, AGENT), 7) == QUOTE.amount
    assert attempts.load_history(conn, AGENT, asdict(QUOTE), guard.POLICY)["seller_status"] is None


@pytest.mark.parametrize("choice,expected", [("no", "denied"), ("cancel", "cancelled")])
def test_refusal_cannot_be_resumed_or_changed_to_yes(conn, choice, expected):
    request_id = pending(conn)
    assert link_approvals.decide(conn, AGENT, request_id, "human", choice)["status"] == expected
    assert accept(conn, request_id)["status"] == expected
    with pytest.raises(payments.PaymentRejected, match="approval"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)
    assert conn.execute("SELECT count(*) AS n FROM approvals").fetchone()["n"] == 1


def test_pending_id_is_not_itself_authorization(conn):
    request_id = pending(conn)
    with pytest.raises(payments.PaymentRejected, match="approval"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)


def test_authorization_can_be_consumed_only_once(conn):
    request_id = pending(conn)
    accept(conn, request_id)
    payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)
    with pytest.raises(payments.PaymentRejected, match="already used"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)
    assert conn.execute("SELECT count(*) AS n FROM approvals").fetchone()["n"] == 1


@pytest.mark.parametrize("authorized", [False, True])
def test_expiry_releases_duplicate_lock_and_refuses_late_yes(conn, authorized):
    request_id = pending(conn)
    if authorized:
        accept(conn, request_id)
    conn.execute("UPDATE payment_attempts SET approval_expires_at = clock_timestamp() - interval '1 second'"
                 " WHERE id = %s", (request_id,))
    assert accept(conn, request_id)["status"] == "expired"
    with pytest.raises(payments.PaymentRejected, match="expired"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)
    assert pending(conn) != request_id


@pytest.mark.parametrize("change", [
    {"amount": 600_000}, {"amount": 400_000}, {"pay_to": "0x" + "b" * 40},
    {"url": "http://seller.test/different"}, {"asset": "0xother"}, {"network": "eip155:1"},
])
def test_changed_quote_invalidates_the_whole_authorization(conn, change):
    request_id = pending(conn)
    accept(conn, request_id)
    with pytest.raises(payments.PaymentRejected, match="quote_changed"):
        payments.authorize(conn, AGENT, REASON, replace(QUOTE, **change), approval_id=request_id)
    assert link_approvals.get(conn, AGENT, request_id)["status"] == "blocked"


def test_limits_and_seller_blocks_are_rechecked_after_yes(conn):
    request_id = pending(conn)
    accept(conn, request_id)
    conn.execute("UPDATE agents SET per_payment_cap = 0.1 WHERE name = %s", (AGENT,))
    with pytest.raises(payments.PaymentRejected, match="cap"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)


def test_explicit_seller_block_never_prompts(conn):
    db.seed_sellers(conn, [{"name": "Blocked", "pay_to": UNKNOWN, "network": QUOTE.network,
                           "asset": QUOTE.asset, "status": "blocked"}])
    with pytest.raises(payments.PaymentRejected, match="seller"):
        payments.authorize(conn, AGENT, REASON, QUOTE, interactive=True)


def test_another_agent_cannot_read_decide_or_consume(conn):
    request_id = pending(conn)
    conn.execute("INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id)"
                 " VALUES ('other', 10, 50, %s)", (db.account_id(conn, "6100"),))
    assert link_approvals.get(conn, "other", request_id) is None
    assert link_approvals.decide(conn, "other", request_id, "attacker", "yes") is None
    with pytest.raises(payments.PaymentRejected, match="approval"):
        payments.authorize(conn, "other", REASON, QUOTE, approval_id=request_id)
    assert link_approvals.get(conn, AGENT, request_id)["status"] == "pending"


def test_second_pending_request_cannot_show_a_second_prompt(conn):
    first = pending(conn)
    with pytest.raises(payments.PaymentRejected, match="duplicate") as caught:
        payments.authorize(conn, AGENT, REASON, QUOTE, interactive=True)
    assert caught.value.related_id == first


def test_yes_cannot_override_a_new_operator_block(conn):
    request_id = pending(conn)
    accept(conn, request_id)
    db.seed_sellers(conn, [{"name": "Blocked", "pay_to": UNKNOWN, "network": QUOTE.network,
                           "asset": QUOTE.asset, "status": "blocked"}])
    with pytest.raises(payments.PaymentRejected, match="seller"):
        payments.authorize(conn, AGENT, REASON, QUOTE, approval_id=request_id)
