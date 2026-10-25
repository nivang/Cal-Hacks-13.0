"""Tests for escrow bookkeeping in ledger.py: hold, then capture or void, against the test schema."""
import pytest
from conftest import ASSET, DATACO, NETWORK

from sentinel import attempts, ledger, payments
from sentinel.guard import Quote
from sentinel.web import users

INFO = {"operator": "0x" + "11" * 20, "salt": "7"}
HASH = "0x" + "ab" * 32


def quote(url: str = "http://x/a") -> Quote:
    return Quote(url, DATACO, 500_000, ASSET, NETWORK, 300)


def hold(conn, agent: str = "research-agent", url: str = "http://x/a") -> int:
    attempt_id = payments.authorize(conn, agent, "why", quote(url))
    ledger.hold_attempt(conn, attempt_id, INFO, HASH + url[-1], 2_000_000_000, 2_000_003_600, "0xauth" + url[-1])
    return attempt_id


def balances(conn) -> dict[str, int]:
    return {r["code"]: r["debit"] - r["credit"] for r in ledger.trial_balance(conn)}


def test_hold_moves_cash_into_escrow_and_keeps_the_lock(conn):
    attempt_id = hold(conn)
    row = conn.execute("SELECT status FROM payment_attempts WHERE id = %s", (attempt_id,)).fetchone()
    escrow = conn.execute("SELECT state, authorize_tx, held FROM escrow_payments WHERE attempt_id = %s",
                          (attempt_id,)).fetchone()
    assert row["status"] == "escrowed"
    assert (escrow["state"], escrow["authorize_tx"], escrow["held"]) == ("authorized", "0xautha", False)
    assert balances(conn)["1050"] == 500_000 and balances(conn)["1010"] == -500_000
    assert attempts.find_live_id(conn, "research-agent", {"pay_to": DATACO, "url": "http://x/a", "amount": 500_000,
                                                          "asset": ASSET, "network": NETWORK, "timeout_s": 300}) is None
    with pytest.raises(payments.PaymentRejected, match="duplicate"):
        payments.authorize(conn, "research-agent", "again", quote())


def test_escrowed_money_counts_as_spent(conn):
    hold(conn)
    aid = conn.execute("SELECT id FROM agents WHERE name = 'research-agent'").fetchone()["id"]
    assert attempts.week_spend(conn, aid, 7) == 500_000


def test_capture_books_the_expense_and_empties_escrow(conn):
    attempt_id = hold(conn)
    ledger.record_delivery(conn, attempt_id, b'{"ok":1}', "application/json", "pass", "delivered", "fine", False)
    ledger.capture_attempt(conn, attempt_id, "0xcap", "sentinel")
    row = conn.execute("SELECT status, tx_hash, delivery_verdict FROM payment_attempts WHERE id = %s",
                       (attempt_id,)).fetchone()
    escrow = conn.execute("SELECT state, capture_tx, decided_by FROM escrow_payments WHERE attempt_id = %s",
                          (attempt_id,)).fetchone()
    assert (row["status"], row["tx_hash"], row["delivery_verdict"]) == ("settled", "0xcap", "pass")
    assert (escrow["state"], escrow["capture_tx"], escrow["decided_by"]) == ("captured", "0xcap", "sentinel")
    assert attempts.load_result(conn, attempt_id) == '{"ok":1}'
    books = balances(conn)
    assert books["1050"] == 0 and books["6100"] == 500_000 and books["1010"] == -500_000


def test_void_returns_the_cash_and_counts_for_nothing(conn):
    attempt_id = hold(conn)
    ledger.record_delivery(conn, attempt_id, "<html>error</html>", "text/html", "fail", "wrong_type", "html", False)
    ledger.void_attempt(conn, attempt_id, "0xvoid", "sentinel")
    row = conn.execute("SELECT status FROM payment_attempts WHERE id = %s", (attempt_id,)).fetchone()
    assert row["status"] == "voided"
    books = balances(conn)
    assert books["1050"] == 0 and books["1010"] == 0 and books.get("6100", 0) == 0
    aid = conn.execute("SELECT id FROM agents WHERE name = 'research-agent'").fetchone()["id"]
    assert attempts.week_spend(conn, aid, 7) == 0
    payments.authorize(conn, "research-agent", "retry", quote())  # the lock is released


def test_capture_or_void_only_from_escrowed(conn):
    attempt_id = hold(conn)
    ledger.void_attempt(conn, attempt_id, "0xvoid", "deadline")
    with pytest.raises(ValueError, match="voided, not escrowed"):
        ledger.capture_attempt(conn, attempt_id, "0xcap", "owner")
    with pytest.raises(ValueError, match="not approved"):
        ledger.hold_attempt(conn, attempt_id, INFO, HASH + "z", 2_000_000_000, 2_000_003_600, "0xauthz")


def test_a_users_agent_holds_in_its_own_escrow_account(conn):
    user = users.find_or_create_user(conn, "escrow@example.com", "escrow@example.com")
    address = "0x" + "cd" * 20
    aid = users.create_books(conn, str(user["id"]), address)
    agent = conn.execute("SELECT name FROM agents WHERE id = %s", (aid,)).fetchone()["name"]
    attempt_id = hold(conn, agent)
    account = conn.execute("SELECT name FROM accounts WHERE code = %s", (f"1050-{aid}",)).fetchone()
    assert account["name"] == f"USDC in Escrow {address}"
    ledger.capture_attempt(conn, attempt_id, "0xcap2", "owner")
    books = balances(conn)
    assert books[f"1050-{aid}"] == 0 and books[f"1010-{aid}"] == -500_000
