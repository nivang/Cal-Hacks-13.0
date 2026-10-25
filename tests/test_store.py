"""Tests for the attempt log and the ledger: the lock, history lookups, balanced and append only books."""
import pytest
from conftest import ASSET, DATACO, NETWORK

from sentinel import attempts, db, guard, ledger

Q = {"url": "http://x/a", "pay_to": DATACO, "amount": 500_000, "asset": ASSET, "network": NETWORK, "timeout_s": 300}


def approve(conn, quote=Q):
    return attempts.record_attempt(conn, "research-agent", quote["url"], None, quote, "approved", "ALLOW", "clean")


def count(conn, table, where=""):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"]


def rejected(conn, sql, error=db.IntegrityError, match=None):
    """Assert a statement fails, inside its own savepoint so the test transaction stays usable."""
    with pytest.raises(error, match=match), db.transaction(conn):
        conn.execute(sql)


def test_in_flight_lock_rejects_second_identical_attempt(conn):
    approve(conn)
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        approve(conn)


def test_lock_is_released_when_attempt_fails(conn):
    first = approve(conn)
    attempts.set_attempt_status(conn, first, "failed")
    approve(conn)  # no UniqueViolation


def test_blocked_attempts_write_rows_and_do_not_hold_the_lock(conn):
    for _ in range(2):
        attempts.record_attempt(conn, "research-agent", Q["url"], None, Q, "blocked", "BLOCK", "cap: x")
    assert count(conn, "payment_attempts", "WHERE status = 'blocked'") == 2


def test_database_rejects_non_positive_amounts(conn):
    zero = {**Q, "amount": 0}
    with pytest.raises(db.IntegrityError), db.transaction(conn):
        attempts.record_attempt(conn, "research-agent", Q["url"], None, zero, "blocked", "BLOCK", "x")


def test_one_transaction_can_settle_only_one_attempt(conn):
    ledger.settle_attempt(conn, approve(conn), "0xsame", b"body")
    other = approve(conn, {**Q, "url": "http://x/b"})
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        ledger.settle_attempt(conn, other, "0xsame", b"body")


def test_history_for_unlisted_seller_is_none(conn):
    h = attempts.load_history(conn, "research-agent", {**Q, "pay_to": "0xother"}, guard.POLICY)
    assert h["seller_status"] is None


def test_history_flags_duplicate_and_baseline_after_settlement(conn):
    attempt_id = approve(conn)
    ledger.settle_attempt(conn, attempt_id, "0xtx", b"body")
    h = attempts.load_history(conn, "research-agent", Q, guard.POLICY)
    assert h == {
        "seller_status": "approved", "week_spend": 500_000, "seller_settled": (500_000,),
        "duplicate": True, "duplicate_of": attempt_id, "lookalike_of": None, "url_paid_to": (Q["pay_to"],),
    }


def test_agent_limits_come_back_in_atomic_units(conn):
    assert attempts.load_agent(conn, "research-agent") == {
        "name": "research-agent", "per_payment_cap": 10_000_000, "weekly_budget": 50_000_000,
        "auto_buy_best_value": False,
    }


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
    lines = conn.execute(
        "SELECT (debit * 1000000)::bigint AS debit, (credit * 1000000)::bigint AS credit"
        " FROM journal_lines WHERE entry_id = %s", (entry_id,),
    ).fetchall()
    assert len(lines) == 2 and sum(r["debit"] for r in lines) == sum(r["credit"] for r in lines) == 500_000
    row = conn.execute("SELECT status, tx_hash, response_hash FROM payment_attempts").fetchone()
    assert (row["status"], row["tx_hash"], row["response_hash"]) == ("settled", "0xtx", ledger.sha256_hex(b"body"))


def test_only_approved_attempts_can_settle_and_nothing_is_written_otherwise(conn):
    blocked = attempts.record_attempt(conn, "research-agent", Q["url"], None, Q, "blocked", "BLOCK", "cap: x")
    with pytest.raises(ValueError):
        ledger.settle_attempt(conn, blocked, "0xtx", b"body")
    assert count(conn, "journal_entries") == 0


def test_unbalanced_entry_is_rejected_before_writing(conn):
    with pytest.raises(ledger.UnbalancedEntry):
        ledger.post_entry(conn, "bad", [("1010", 5, 0), ("6100", 0, 4)])
    assert count(conn, "journal_entries") == 0


def test_a_failure_inside_settlement_rolls_back_the_status_change(conn, monkeypatch):
    attempt_id = approve(conn)
    monkeypatch.setattr(ledger, "post_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ledger.settle_attempt(conn, attempt_id, "0xtx", b"body")
    assert conn.execute("SELECT status FROM payment_attempts").fetchone()["status"] == "approved"


# Ledger integrity enforced by the database itself

def test_settlement_stores_the_body_and_its_hash(conn):
    attempt_id = approve(conn)
    ledger.settle_attempt(conn, attempt_id, "0xtx", b'{"open_rate": 0.214}', "application/json")
    assert attempts.load_result(conn, attempt_id) == '{"open_rate": 0.214}'
    row = conn.execute("SELECT content_type FROM attempt_results WHERE attempt_id = %s", (attempt_id,)).fetchone()
    assert row["content_type"] == "application/json"


def test_body_with_nul_bytes_and_bad_utf8_still_settles(conn):
    attempt_id = approve(conn)
    ledger.settle_attempt(conn, attempt_id, "0xtx", b"a\x00b\xff")
    assert attempts.load_result(conn, attempt_id) == "a\ufffdb\ufffd"
    assert count(conn, "journal_entries") == 1


def test_a_failed_attempt_has_no_stored_body(conn):
    attempt_id = approve(conn)
    attempts.set_attempt_status(conn, attempt_id, "failed")
    assert attempts.load_result(conn, attempt_id) is None


def test_get_attempt_is_scoped_to_the_calling_agent(conn):
    attempt_id = approve(conn)
    assert attempts.get_attempt(conn, attempt_id, "research-agent")["status"] == "approved"
    db.init(conn)
    conn.execute(
        "INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id)"
        " VALUES ('other-agent', 1, 1, %s)", (db.account_id(conn, "6100"),),
    )
    assert attempts.get_attempt(conn, attempt_id, "other-agent") is None
    assert attempts.get_attempt(conn, 999_999_999, "research-agent") is None


def test_find_live_id_returns_the_attempt_holding_the_lock(conn):
    attempt_id = approve(conn)
    assert attempts.find_live_id(conn, "research-agent", Q) == attempt_id
    attempts.set_attempt_status(conn, attempt_id, "failed")
    assert attempts.find_live_id(conn, "research-agent", Q) is None


def test_ledger_tables_reject_update_delete_and_truncate(conn):
    entry_id = ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    conn.execute(
        "INSERT INTO approvals (attempt_id, approver, decision) SELECT id, 'human', 'approved' FROM payment_attempts"
    )
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")  # run the balance checks now so TRUNCATE has no pending events
    for table in ("journal_entries", "journal_lines", "approvals", "attempt_results"):
        key = "attempt_id" if table == "attempt_results" else "id"
        rejected(conn, f"UPDATE {table} SET {key} = {key}", match="append only")
        rejected(conn, f"DELETE FROM {table}", match="append only")
        rejected(conn, f"TRUNCATE {table} CASCADE", match="append only")
    assert count(conn, "journal_entries", f"WHERE id = {entry_id}") == 1


def test_unbalanced_entry_is_rejected_at_commit(test_schema):
    """Needs a real commit, so it runs outside the rollback fixture. A rejected commit leaves nothing behind."""
    with db.connection() as c:
        with pytest.raises(db.IntegrityError, match="unbalanced"), db.transaction(c):
            db.use_schema(c, test_schema)
            entry = db.insert(c, "INSERT INTO journal_entries (memo) VALUES (%s)", ("unbalanced at commit",))
            for code, debit, credit in [("1010", 5, 0), ("6100", 0, 4)]:
                c.execute(
                    "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, %s, %s)",
                    (entry, db.account_id(c, code), debit, credit),
                )
        with db.transaction(c, rollback=True):
            db.use_schema(c, test_schema)
            assert count(c, "journal_entries", "WHERE memo = 'unbalanced at commit'") == 0


def test_entry_with_no_lines_is_rejected_at_commit(test_schema):
    with db.connection() as c:
        with pytest.raises(db.IntegrityError, match="unbalanced"), db.transaction(c):
            db.use_schema(c, test_schema)
            c.execute("INSERT INTO journal_entries (memo) VALUES ('empty at commit')")


def test_reversal_nets_the_original_to_zero(conn):
    entry_id = ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    ledger.reverse_entry(conn, entry_id, "test")
    assert all(r["debit"] == r["credit"] for r in ledger.trial_balance(conn))
    assert count(conn, "journal_entries", f"WHERE reverses_entry_id = {entry_id}") == 1


def test_trial_balance_balances_after_funding_and_payment(conn):
    ledger.post_funding(conn, 9_000_000, "opening")
    ledger.settle_attempt(conn, approve(conn), "0xtx", b"body")
    rows = ledger.trial_balance(conn)
    assert sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows) == 9_500_000
    assert ledger.has_entries(conn)
