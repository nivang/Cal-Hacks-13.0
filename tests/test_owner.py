"""Tests for the owner's actions and the ledger queries: the seller list really is what the guard obeys."""
import csv
import io

import pytest
from conftest import ASSET, DATACO, NETWORK

from sentinel import ledger, owner, payments
from sentinel.guard import Quote

NEW_SELLER = "0x1111111111111111111111111111111111111111"


def quote(amount: int = 500_000, pay_to: str = DATACO, url: str = "http://x/a") -> Quote:
    return Quote(url, pay_to, amount, ASSET, NETWORK, 300)


@pytest.mark.parametrize("text, atomic", [
    ("$0.50", 500_000), ("0.5", 500_000), ("10", 10_000_000), (" $1,000.00 ", 1_000_000_000), ("0.000001", 1),
])
def test_parse_usd_reads_dollars_into_atomic_units(text, atomic):
    assert owner.parse_usd(text) == atomic


@pytest.mark.parametrize("text", ["", "abc", "0", "-1", "$-0.50", "0.0000001", "NaN", "Infinity", "1e400"])
def test_parse_usd_rejects_what_is_not_a_positive_usdc_amount(text):
    with pytest.raises(ValueError):
        owner.parse_usd(text)


def test_approving_a_new_seller_lets_the_agent_pay_it(conn):
    with pytest.raises(payments.PaymentRejected, match="seller"):
        payments.authorize(conn, "research-agent", "why", quote(pay_to=NEW_SELLER))
    owner.upsert_seller(conn, "NewCo", NEW_SELLER)
    assert payments.authorize(conn, "research-agent", "why", quote(pay_to=NEW_SELLER, url="http://x/b"))


def test_blocking_a_seller_stops_payments_and_matches_any_letter_case(conn):
    seller_id = owner.upsert_seller(conn, "DataCo", DATACO.upper().replace("0X", "0x"), "blocked")
    rows = conn.execute(
        "SELECT id, status, added_by FROM sellers WHERE lower(pay_to) = lower(%s)", (DATACO,)
    ).fetchall()
    assert [(r["id"], r["status"], r["added_by"]) for r in rows] == [(seller_id, "blocked", "owner")]
    with pytest.raises(payments.PaymentRejected) as e:
        payments.authorize(conn, "research-agent", "why", quote())
    assert e.value.rule == "seller" and "blocked list" in e.value.detail


@pytest.mark.parametrize("name, pay_to, status", [
    ("NewCo", "0x123", "approved"),
    ("NewCo", "1111111111111111111111111111111111111111", "approved"),
    ("", NEW_SELLER, "approved"),
    ("NewCo", NEW_SELLER, "pending"),
])
def test_bad_seller_input_is_rejected(conn, name, pay_to, status):
    with pytest.raises(ValueError):
        owner.upsert_seller(conn, name, pay_to, status)


def test_new_limits_change_what_the_guard_allows(conn):
    owner.set_agent_limits(conn, "research-agent", 400_000, 5_000_000)
    with pytest.raises(payments.PaymentRejected, match="cap"):
        payments.authorize(conn, "research-agent", "why", quote(500_000))
    agent = next(a for a in owner.list_agents(conn, 7) if a["name"] == "research-agent")
    assert (agent["per_payment_cap"], agent["weekly_budget"]) == (400_000, 5_000_000)


def test_limits_must_make_sense(conn):
    with pytest.raises(ValueError):
        owner.set_agent_limits(conn, "research-agent", 20_000_000, 10_000_000)
    with pytest.raises(LookupError):
        owner.set_agent_limits(conn, "nobody", 1, 1)


def test_week_spend_on_the_agents_list_counts_settled_payments(conn):
    ledger.settle_attempt(conn, payments.authorize(conn, "research-agent", "why", quote()), "0xtx1", b"body")
    agent = next(a for a in owner.list_agents(conn, 7) if a["name"] == "research-agent")
    assert agent["week_spend"] == 500_000


def test_attempts_list_names_the_seller_and_splits_the_rule(conn):
    payments.authorize(conn, "research-agent", "need benchmarks", quote())
    with pytest.raises(payments.PaymentRejected):
        payments.authorize(conn, "research-agent", "trap", quote(pay_to=NEW_SELLER))
    blocked, allowed = owner.list_attempts(conn)
    assert (allowed["seller"], allowed["agent"], allowed["amount"], allowed["rule"]) == (
        "DataCo", "research-agent", 500_000, "clean")
    assert blocked["seller"] is None and blocked["rule"] == "seller" and NEW_SELLER in blocked["detail"]


def test_journal_list_has_balanced_lines_per_entry(conn):
    ledger.settle_attempt(conn, payments.authorize(conn, "research-agent", "why", quote()), "0xtx2", b"body")
    entry = owner.list_journal(conn)[0]
    assert [(line["code"], line["debit"], line["credit"]) for line in entry["lines"]] == [
        ("6100", 500_000, 0), ("1010", 0, 500_000)]


def test_payments_csv_has_one_row_per_attempt_and_defuses_formulas(conn):
    payments.authorize(conn, "research-agent", '=HYPERLINK("http://evil","click")', quote())
    rows = list(csv.DictReader(io.StringIO(owner.payments_csv(owner.list_attempts(conn)))))
    assert len(rows) == 1 and list(rows[0]) == owner.CSV_COLUMNS
    assert rows[0]["reason"].startswith("'=HYPERLINK") and rows[0]["amount_usdc"] == "0.50"
    assert rows[0]["seller"] == "DataCo" and rows[0]["decision"] == "ALLOW"
