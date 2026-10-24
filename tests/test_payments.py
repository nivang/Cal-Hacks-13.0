"""Tests for the pieces of payments.py that decide and record, with no network."""
from types import SimpleNamespace

import pytest
from conftest import ASSET, DATACO, NETWORK, SCAMMER

from sentinel import ledger, payments
from sentinel.guard import Quote


def requirement(network: str = NETWORK, asset: str = ASSET, amount: str = "500000"):
    return SimpleNamespace(network=network, asset=asset, amount=amount, pay_to=DATACO, max_timeout_seconds=300)


def quote(amount: int = 500_000, pay_to: str = DATACO, url: str = "http://x/a") -> Quote:
    return Quote(url, pay_to, amount, ASSET, NETWORK, 300)


def test_select_requirement_picks_base_sepolia_usdc():
    wrong = requirement(network="eip155:1")
    right = requirement()
    assert payments.select_requirement(SimpleNamespace(accepts=[wrong, right])) is right


def test_select_requirement_rejects_when_nothing_matches():
    nothing_matches = [requirement(network="eip155:1"), requirement(asset="0xother")]
    with pytest.raises(payments.PaymentRejected):
        payments.select_requirement(SimpleNamespace(accepts=nothing_matches))


def test_build_quote_keeps_amount_as_an_integer():
    q = payments.build_quote("http://x/a", requirement(amount="2000000"))
    assert q.amount == 2_000_000 and isinstance(q.amount, int) and q.timeout_s == 300


def test_authorize_allows_a_clean_quote_and_records_approved(conn):
    attempt_id = payments.authorize(conn, "research-agent", "why", quote())
    row = conn.execute("SELECT status, decision FROM payment_attempts WHERE id = %s", (attempt_id,)).fetchone()
    assert (row["status"], row["decision"]) == ("approved", "ALLOW")


def test_authorize_blocks_over_cap_and_still_writes_a_row(conn):
    with pytest.raises(payments.PaymentRejected, match="cap") as e:
        payments.authorize(conn, "research-agent", None, quote(amount=15_000_000))
    row = conn.execute("SELECT id, status FROM payment_attempts").fetchone()
    assert row["status"] == "blocked"
    assert (e.value.rule, e.value.attempt_id) == ("cap", row["id"])
    assert "$15.00" in e.value.detail


def test_authorize_blocks_an_unlisted_seller_and_signs_nothing(conn):
    with pytest.raises(payments.PaymentRejected) as e:
        payments.authorize(conn, "research-agent", None, quote(amount=9_000_000, pay_to=SCAMMER, url="http://x/premium"))
    row = conn.execute("SELECT id, status, decision FROM payment_attempts").fetchone()
    assert (row["status"], row["decision"]) == ("blocked", "BLOCK")
    assert e.value.attempt_id == row["id"] and e.value.rule == "seller"
    assert "Only the owner can add sellers" in e.value.detail


def test_authorize_blocks_a_second_identical_payment_in_flight(conn):
    first = payments.authorize(conn, "research-agent", None, quote())
    with pytest.raises(payments.PaymentRejected, match="duplicate") as e:
        payments.authorize(conn, "research-agent", None, quote())
    assert conn.execute("SELECT COUNT(*) AS n FROM payment_attempts WHERE status = 'blocked'").fetchone()["n"] == 1
    assert e.value.related_id == first and e.value.attempt_id != first


def test_authorize_blocks_a_price_surge_after_a_settled_purchase(conn):
    ledger.settle_attempt(conn, payments.authorize(conn, "research-agent", None, quote()), "0xtx", b"body")
    with pytest.raises(payments.PaymentRejected, match="price_jump") as e:
        payments.authorize(conn, "research-agent", None, quote(amount=2_000_000))
    assert "4.0x" in e.value.detail


def test_repeating_an_unlisted_seller_logs_each_block_and_takes_no_lock(conn):
    ids = []
    for _ in range(2):
        with pytest.raises(payments.PaymentRejected) as e:
            payments.authorize(conn, "research-agent", None, quote(amount=9_000_000, pay_to=SCAMMER, url="http://x/premium"))
        ids.append(e.value.attempt_id)
    assert conn.execute("SELECT COUNT(*) AS n FROM payment_attempts WHERE status = 'blocked'").fetchone()["n"] == 2
    assert ids[0] != ids[1] and all(e.value.rule == "seller" for _ in ids)
