"""Tests for seller lists per user: the operator's list plus each user's own, as the guard and the books use them.

Covers attempts.load_history and payments.authorize (a personal approval opens a seller for that user only, the
operator's approval applies to everyone, a user's block beats an operator approval for that user alone, an operator
block cannot be overridden), the legacy research-agent, owner.list_attempts and the CSV (one row per attempt even when
several seller rows share an address), and web/books.py (merged seller list, validation, per user journal, totals,
summary, live grant, demo sellers). Everything runs on the test connection; no network.
"""
import csv
import io

import pytest
from conftest import ASSET, DATACO, NETWORK, access_token_for, fake_wallet_address

from sentinel import attempts, guard, ledger, owner, payments
from sentinel.guard import Quote
from sentinel.web import books, users

NEW_SELLER = "0x2222222222222222222222222222222222222222"


def make_user(conn, email):
    """A user with a wallet and books: returns (agent id, agent name, wallet address)."""
    user = users.find_or_create_user(conn, email, email.lower())
    address = fake_wallet_address(user["id"])
    agent_id = users.create_books(conn, user["id"], address)
    return agent_id, users.agent_name(user["id"]), address


def quote(pay_to=NEW_SELLER, url="http://x/a", amount=500_000):
    return Quote(url, pay_to, amount, ASSET, NETWORK, 300)


def blocked_by_seller(conn, agent, pay_to=NEW_SELLER):
    """True when the guard blocks this agent's payment on the seller rule."""
    try:
        payments.authorize(conn, agent, "why", quote(pay_to=pay_to, url=f"http://x/{agent[-4:]}"))
    except payments.PaymentRejected as e:
        return e.rule == "seller"
    return False


# The guard


def test_a_personal_approval_opens_the_seller_for_that_user_only(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    _, b, _ = make_user(conn, "b@example.com")
    books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER)
    assert not blocked_by_seller(conn, a)
    assert blocked_by_seller(conn, b)
    assert blocked_by_seller(conn, "research-agent")


def test_an_operator_approval_applies_to_every_agent(conn):
    _, a, _ = make_user(conn, "a@example.com")
    _, b, _ = make_user(conn, "b@example.com")
    owner.upsert_seller(conn, "NewCo", NEW_SELLER)
    assert not blocked_by_seller(conn, a) and not blocked_by_seller(conn, b)
    assert not blocked_by_seller(conn, "research-agent")


def test_a_users_own_block_beats_an_operator_approval_for_that_user_only(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    _, b, _ = make_user(conn, "b@example.com")
    owner.upsert_seller(conn, "NewCo", NEW_SELLER)
    books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER, "blocked")
    assert blocked_by_seller(conn, a)
    assert not blocked_by_seller(conn, b)


def test_an_operator_block_cannot_be_overridden_by_a_personal_approval(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    owner.upsert_seller(conn, "NewCo", NEW_SELLER, "blocked")
    books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER, "approved")
    assert blocked_by_seller(conn, a)
    status = attempts.load_history(conn, a, quote().__dict__, guard.POLICY)["seller_status"]
    assert status == "blocked"


def test_seller_status_is_blocked_then_approved_then_none(conn):
    a_id, a, _ = make_user(conn, "a@example.com")

    def status():
        return attempts.load_history(conn, a, quote().__dict__, guard.POLICY)["seller_status"]

    assert status() is None
    books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER)
    assert status() == "approved"
    books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER, "blocked")
    assert status() == "blocked"


def test_the_legacy_research_agent_still_follows_the_operator_list(conn):
    assert payments.authorize(conn, "research-agent", "why", quote(pay_to=DATACO))
    owner.upsert_seller(conn, "DataCo", DATACO, "blocked")
    with pytest.raises(payments.PaymentRejected, match="seller"):
        payments.authorize(conn, "research-agent", "why", quote(pay_to=DATACO, url="http://x/b"))


def test_new_limits_from_the_user_change_what_the_guard_allows(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    owner.set_agent_limits(conn, a, 400_000, 5_000_000)
    with pytest.raises(payments.PaymentRejected) as e:
        payments.authorize(conn, a, "why", quote(pay_to=DATACO))
    assert e.value.rule == "cap"


# The ledger queries


def test_an_attempt_is_listed_once_even_when_several_seller_rows_share_its_address(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    b_id, b, _ = make_user(conn, "b@example.com")
    books.upsert_seller(conn, a_id, "A calls it Mine", DATACO)  # DataCo is also on the operator list
    books.upsert_seller(conn, b_id, "B calls it Theirs", DATACO)
    first = payments.authorize(conn, a, "why", quote(pay_to=DATACO, url="http://x/a"))
    second = payments.authorize(conn, b, "why", quote(pay_to=DATACO, url="http://x/b"))
    rows = owner.list_attempts(conn)
    ids = [r["id"] for r in rows]
    assert ids.count(first) == 1 and ids.count(second) == 1 and len(ids) == len(set(ids))
    by_id = {r["id"]: r for r in rows}
    assert by_id[first]["seller"] == "A calls it Mine" and by_id[second]["seller"] == "B calls it Theirs"
    body = list(csv.reader(io.StringIO(owner.payments_csv(rows))))
    assert len(body) == 1 + len(rows)  # a header and one row per attempt


def test_the_operator_row_names_the_seller_when_the_agent_has_no_row_of_its_own(conn):
    _, a, _ = make_user(conn, "a@example.com")
    attempt = payments.authorize(conn, a, "why", quote(pay_to=DATACO))
    row = next(r for r in owner.list_attempts(conn) if r["id"] == attempt)
    assert row["seller"] == "DataCo" and row["seller_listed_by"] == "human"


def test_the_operator_list_functions_leave_personal_rows_alone(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    books.upsert_seller(conn, a_id, "Mine", NEW_SELLER)
    owner.upsert_seller(conn, "Operator", NEW_SELLER, "blocked")
    assert [s["pay_to"] for s in owner.list_sellers(conn) if s["pay_to"] == NEW_SELLER] == [NEW_SELLER]
    mine = conn.execute("SELECT name, status FROM sellers WHERE agent_id = %s", (a_id,)).fetchone()
    assert (mine["name"], mine["status"]) == ("Mine", "approved")
    assert conn.execute("SELECT count(*) AS n FROM sellers WHERE agent_id IS NULL AND pay_to = %s",
                        (NEW_SELLER,)).fetchone()["n"] == 1


# books.py


def test_upsert_seller_validates_and_refuses_the_users_own_wallet(conn):
    a_id, _, wallet = make_user(conn, "a@example.com")
    for name, pay_to, status in [
        ("", NEW_SELLER, "approved"), ("x" * 101, NEW_SELLER, "approved"), ("NewCo", "0x123", "approved"),
        ("NewCo", NEW_SELLER, "pending"), ("NewCo", wallet, "approved"),
        ("NewCo", wallet.upper().replace("0X", "0x"), "approved"),
    ]:
        with pytest.raises(ValueError):
            books.upsert_seller(conn, a_id, name, pay_to, status)
    assert conn.execute("SELECT count(*) AS n FROM sellers WHERE agent_id = %s", (a_id,)).fetchone()["n"] == 0


def test_upsert_seller_updates_the_same_row_whatever_the_letter_case(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    first = books.upsert_seller(conn, a_id, "NewCo", NEW_SELLER)
    second = books.upsert_seller(conn, a_id, "  Renamed ", NEW_SELLER.upper().replace("0X", "0x"), "blocked")
    assert first == second
    row = conn.execute("SELECT name, status, added_by FROM sellers WHERE id = %s", (first,)).fetchone()
    assert (row["name"], row["status"], row["added_by"]) == ("Renamed", "blocked", "user")


def test_list_sellers_merges_the_lists_with_the_winning_status_and_source(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    b_id, _, _ = make_user(conn, "b@example.com")
    books.upsert_seller(conn, a_id, "Mine", NEW_SELLER)
    books.upsert_seller(conn, a_id, "Muted", DATACO, "blocked")  # a block over the operator's approval
    books.upsert_seller(conn, b_id, "Other", SCAMMER_ADDRESS)
    mine = {s["pay_to"]: s for s in books.list_sellers(conn, a_id)}
    assert (mine[NEW_SELLER]["status"], mine[NEW_SELLER]["source"]) == ("approved", "you")
    assert (mine[DATACO]["status"], mine[DATACO]["source"], mine[DATACO]["own_status"]) == ("blocked", "you", "blocked")
    assert SCAMMER_ADDRESS not in mine
    theirs = {s["pay_to"]: s for s in books.list_sellers(conn, b_id)}
    assert (theirs[DATACO]["status"], theirs[DATACO]["source"], theirs[DATACO]["own_status"]) == (
        "approved", "operator", None)


def test_an_operator_block_shows_as_blocked_by_the_operator_whatever_the_user_chose(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    owner.upsert_seller(conn, "DataCo", DATACO, "blocked")
    books.upsert_seller(conn, a_id, "DataCo", DATACO, "approved")
    row = next(s for s in books.list_sellers(conn, a_id) if s["pay_to"] == DATACO)
    assert (row["status"], row["source"]) == ("blocked", "operator")


SCAMMER_ADDRESS = "0x3333333333333333333333333333333333333333"


def settle_one(conn, agent, url, amount=500_000):
    attempt = payments.authorize(conn, agent, "why", quote(pay_to=DATACO, url=url, amount=amount))
    ledger.settle_attempt(conn, attempt, f"0xtx{attempt}", b"body")
    return attempt


def test_journal_totals_and_summary_are_scoped_to_one_agent(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    b_id, b, _ = make_user(conn, "b@example.com")
    settle_one(conn, a, "http://x/one")
    settle_one(conn, a, "http://x/two", 250_000)
    settle_one(conn, b, "http://x/three")
    entries = books.journal(conn, a_id)
    assert [e["memo"].split()[2] for e in entries] == ["http://x/two", "http://x/one"]
    assert all(sum(line["debit"] for line in e["lines"]) == sum(line["credit"] for line in e["lines"]) for e in entries)
    totals = books.totals(conn, a_id)
    assert totals["balanced"] and totals["debit"] == totals["credit"] == 750_000
    assert {r["code"] for r in totals["rows"]} == {"6100", f"1010-{a_id}"}
    assert books.summary(conn, a_id) == {"settled": 2, "spent": 750_000, "week_spend": 750_000}
    assert books.summary(conn, b_id)["spent"] == 500_000
    empty_id, _, _ = make_user(conn, "c@example.com")
    assert books.journal(conn, empty_id) == []
    assert books.totals(conn, empty_id) == {"rows": [], "debit": 0, "credit": 0, "balanced": True}


def test_list_payments_is_scoped_newest_first_and_recent_payments_is_the_head(conn):
    a_id, a, _ = make_user(conn, "a@example.com")
    _, b, _ = make_user(conn, "b@example.com")
    ids = [payments.authorize(conn, a, "why", quote(pay_to=DATACO, url=f"http://x/{i}")) for i in range(7)]
    payments.authorize(conn, b, "why", quote(pay_to=DATACO, url="http://x/b"))
    listed = books.list_payments(conn, a_id)
    assert [r["id"] for r in listed] == ids[::-1]
    assert [r["id"] for r in books.recent_payments(conn, a_id)] == ids[::-1][:5]


def test_demo_sellers_only_in_dev_mode_never_the_scammer_and_hidden_once_approved(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    env = {"SELLER_A_ADDRESS": NEW_SELLER, "SELLER_B_ADDRESS": "0x" + "4" * 40, "SELLER_C_ADDRESS": SCAMMER_ADDRESS}
    assert books.demo_sellers(conn, a_id, False, env) == []
    shown = books.demo_sellers(conn, a_id, True, env)
    assert [d["name"] for d in shown] == ["DataCo", "InsightLab"]
    assert SCAMMER_ADDRESS not in [d["pay_to"] for d in shown]
    books.upsert_seller(conn, a_id, "DataCo", NEW_SELLER)
    assert [d["name"] for d in books.demo_sellers(conn, a_id, True, env)] == ["InsightLab"]
    assert books.demo_sellers(conn, a_id, True, {"SELLER_A_ADDRESS": "nonsense"}) == []


def test_has_live_grant_needs_an_exchanged_unrevoked_unexpired_grant(conn, settings):
    user = users.find_or_create_user(conn, "a@example.com", "a@example.com")
    users.create_books(conn, user["id"], fake_wallet_address(user["id"]))
    assert not books.has_live_grant(conn, user["id"])
    identity = users.identity_for(conn, user["id"])
    assert identity is not None
    access_token_for(conn, settings, identity)
    conn.execute("UPDATE oauth_codes SET used_at = now() WHERE user_id = %s", (user["id"],))
    assert books.has_live_grant(conn, user["id"])
    conn.execute("UPDATE oauth_codes SET grant_expires_at = now() - interval '1 minute' WHERE user_id = %s",
                 (user["id"],))
    assert not books.has_live_grant(conn, user["id"])
    conn.execute("UPDATE oauth_codes SET grant_expires_at = now() + interval '1 day', revoked_at = now()"
                 " WHERE user_id = %s", (user["id"],))
    assert not books.has_live_grant(conn, user["id"])


def test_every_why_label():
    from sentinel.web.pages import why
    labels = {"seller": "Seller not approved", "cap": "Over your per payment cap", "budget": "Over your weekly budget",
              "price_jump": "Price jumped", "duplicate": "Duplicate payment", "url": "Unsafe URL",
              "payment_option": "No supported payment option", "weird_rule": "Weird Rule"}
    for rule, label in labels.items():
        assert why({"status": "blocked", "rule": rule, "detail": "Because."}) == (label, "Because.")
    assert why({"status": "denied", "rule": "cap", "detail": None})[0] == "Over your per payment cap"
    assert why({"status": "failed", "rule": "", "detail": "", "tx_hash": None}) == ("Payment did not complete", "")
    assert why({"status": "failed", "tx_hash": "0xabc"}) == ("Paid but not recorded, contact the owner", "")
    assert why({"status": "approved"}) == ("In progress", "") and why({"status": "pending"}) == ("In progress", "")
    assert why({"status": "settled", "tx_hash": "0x1"}) == ("", "")


def test_an_operator_block_is_the_source_even_when_the_user_blocked_it_too(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    owner.upsert_seller(conn, "Op", NEW_SELLER, "blocked")
    books.upsert_seller(conn, a_id, "Mine", NEW_SELLER, "blocked")
    [row] = [s for s in books.list_sellers(conn, a_id) if s["pay_to"].lower() == NEW_SELLER]
    assert (row["status"], row["source"], row["own_status"]) == ("blocked", "operator", "blocked")


def test_the_personal_list_is_capped_for_new_addresses_only_and_listing_reads_at_most_the_cap(conn):
    a_id, _, _ = make_user(conn, "a@example.com")
    for i in range(books.SELLER_LIMIT + 5):
        conn.execute(
            "INSERT INTO sellers (name, pay_to, network, asset, status, added_by, agent_id)"
            " VALUES (%s, %s, %s, %s, 'approved', 'user', %s)", (f"S{i}", f"0x{i + 1:040x}", NETWORK, ASSET, a_id),
        )
    assert len([s for s in books.list_sellers(conn, a_id) if s["source"] == "you"]) == books.SELLER_LIMIT
    with pytest.raises(ValueError, match="up to 200 sellers"):
        books.upsert_seller(conn, a_id, "New", NEW_SELLER)
    books.upsert_seller(conn, a_id, "Changed", f"0x{1:040x}".upper().replace("0X", "0x"), "blocked")  # existing: fine
