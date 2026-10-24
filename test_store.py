"""Tests for the attempt log and the ledger: the lock, history lookups, balanced and append only books."""
import sqlite3

import pytest

import attempts
import guard
import ledger
from conftest import ASSET, DATACO, NETWORK

Q = {"url": "http://x/a", "pay_to": DATACO, "amount": 500_000, "asset": ASSET, "network": NETWORK, "timeout_s": 300}


def approve(conn, quote=Q):
    return attempts.record_attempt(conn, "research-agent", quote["url"], None, quote, "approved", "ALLOW", "clean")


def test_in_flight_lock_rejects_second_identical_attempt(conn):
    approve(conn)
    with pytest.raises(sqlite3.IntegrityError):
        approve(conn)


def test_lock_is_released_when_attempt_fails(conn):
    first = approve(conn)
    attempts.set_attempt_status(conn, first, "failed")
    approve(conn)  # no IntegrityError


def test_blocked_attempts_write_rows_and_do_not_hold_the_lock(conn):
    for _ in range(2):
        attempts.record_attempt(conn, "research-agent", Q["url"], None, Q, "blocked", "BLOCK", "cap: x")
    assert conn.execute("SELECT COUNT(*) FROM payment_attempts WHERE status = 'blocked'").fetchone()[0] == 2


def test_history_for_unlisted_seller_is_none(conn):
    h = attempts.load_history(conn, "research-agent", {**Q, "pay_to": "0xother"}, guard.POLICY)
    assert h["seller_status"] is None


def test_history_flags_duplicate_and_baseline_after_settlement(conn):
    ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    h = attempts.load_history(conn, "research-agent", Q, guard.POLICY)
    assert h == {"seller_status": "approved", "week_spend": 500_000, "seller_settled": (500_000,), "duplicate": True}


def test_in_flight_spend_counts_toward_the_budget(conn):
    approve(conn)
    assert attempts.load_history(conn, "research-agent", Q, guard.POLICY)["week_spend"] == 500_000


def test_blocked_attempts_do_not_count_as_spend(conn):
    attempts.record_attempt(conn, "research-agent", Q["url"], None, Q, "blocked", "BLOCK", "cap: x")
    assert attempts.load_history(conn, "research-agent", Q, guard.POLICY)["week_spend"] == 0


def test_price_history_uses_only_the_last_five_settled(conn):
    for i in range(7):
        ledger.settle_attempt(conn, approve(conn, {**Q, "amount": 100_000 * (i + 1)}), f"0x{i}", b"b")
    seller_settled = attempts.load_history(conn, "research-agent", Q, guard.POLICY)["seller_settled"]
    assert seller_settled == (700_000, 600_000, 500_000, 400_000, 300_000)


def test_settlement_posts_a_balanced_two_line_entry(conn):
    entry_id = ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    lines = conn.execute("SELECT debit, credit FROM journal_lines WHERE entry_id = ?", (entry_id,)).fetchall()
    assert len(lines) == 2 and sum(r["debit"] for r in lines) == sum(r["credit"] for r in lines) == 500_000
    row = conn.execute("SELECT status, tx_hash, response_hash FROM payment_attempts").fetchone()
    assert (row["status"], row["tx_hash"], row["response_hash"]) == ("settled", "0xtx", ledger.sha256_hex(b"body"))


def test_only_approved_attempts_can_settle_and_nothing_is_written_otherwise(conn):
    blocked = attempts.record_attempt(conn, "research-agent", Q["url"], None, Q, "blocked", "BLOCK", "cap: x")
    with pytest.raises(ValueError):
        ledger.settle_attempt(conn, blocked, "0xtx", b"body")
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_unbalanced_entry_is_rejected_and_writes_nothing(conn):
    with pytest.raises(ledger.UnbalancedEntry):
        ledger.post_entry(conn, "bad", [("1010", 5, 0), ("6100", 0, 4)])
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_a_failure_inside_settlement_rolls_back_the_status_change(conn, monkeypatch):
    attempt_id = approve(conn)
    monkeypatch.setattr(ledger, "post_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ledger.settle_attempt(conn, attempt_id, "0xtx", b"body")
    assert conn.execute("SELECT status FROM payment_attempts").fetchone()[0] == "approved"


def test_journal_is_append_only(conn):
    entry_id = ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    for sql in ("UPDATE journal_lines SET debit = 1", "DELETE FROM journal_lines",
                f"UPDATE journal_entries SET memo = 'x' WHERE id = {entry_id}", "DELETE FROM journal_entries"):
        with pytest.raises(sqlite3.DatabaseError, match="append only"):
            conn.execute(sql)


def test_reversal_nets_the_original_to_zero(conn):
    entry_id = ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    ledger.reverse_entry(conn, entry_id, "test")
    assert all(r["debit"] == r["credit"] for r in ledger.trial_balance(conn))
    reversals = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE reverses_entry_id = ?", (entry_id,))
    assert reversals.fetchone()[0] == 1


def test_trial_balance_balances_after_funding_and_payment(conn):
    ledger.post_funding(conn, 9_000_000, "opening")
    ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    rows = ledger.trial_balance(conn)
    assert sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows) == 9_500_000
    assert ledger.has_entries(conn)
