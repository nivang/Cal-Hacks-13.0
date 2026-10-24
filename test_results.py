"""Tests for the result shape and wording the agent sees. Pure: no database, no server."""
import results
from guard import Quote
from payments import PayResult

QUOTE = Quote("http://x/a", "0xSeller", 500_000, "0xusdc", "eip155:84532", 300)


def test_usdc_text_has_at_least_two_places():
    assert [results.usdc_text(n) for n in (500_000, 1_500_000, 123_456, 9_000_000, 0)] == [
        "0.50", "1.50", "0.123456", "9.00", "0.00",
    ]


def test_settled_result_has_body_amount_tx_and_link():
    r = results.from_pay_result(PayResult(200, '{"a": 1}', "0xabc", QUOTE, attempt_id=7, settled=True))
    assert (r.status, r.request_id, r.body, r.amount, r.amount_usdc, r.tx_hash) == (
        "SETTLED", 7, '{"a": 1}', 500_000, "0.50", "0xabc")
    assert r.explorer_url == "https://sepolia.basescan.org/tx/0xabc" and r.seller == "0xSeller"
    assert "third party" in r.message


def test_long_body_is_clipped_and_flagged():
    r = results.from_pay_result(PayResult(200, "x" * (results.MAX_BODY_CHARS + 5), "0xabc", QUOTE, 7, True))
    assert r.body is not None and len(r.body) == results.MAX_BODY_CHARS and r.body_truncated


def test_free_resource_is_free_with_no_request_id():
    r = results.from_pay_result(PayResult(200, "hello"))
    assert (r.status, r.request_id, r.body, r.amount) == ("FREE", None, "hello", None)


def test_seller_error_without_payment_is_failed_and_says_nothing_was_paid():
    r = results.from_pay_result(PayResult(500, "oops", None, QUOTE, attempt_id=9, settled=False))
    assert r.status == "FAILED" and r.request_id == 9 and "no payment was made" in r.message


def test_blocked_result_carries_rule_and_plain_reason():
    r = results.blocked(12, "price_jump", "This seller's price is 4.0x its usual ($2.00 vs a typical $0.50).")
    assert (r.status, r.request_id, r.rule) == ("BLOCKED", 12, "price_jump")
    assert "4.0x" in r.message and "Do not retry" in r.message


def test_duplicate_block_points_at_the_earlier_payment():
    r = results.blocked(13, "duplicate", "Already paid (request 7).", related_id=7)
    assert r.related_request_id == 7 and "payment_status(7)" in r.message and "Do not retry" not in r.message


def test_pending_result_says_wait_and_not_to_resubmit():
    r = results.pending(5, "seller", "The seller 0xabc is not on the approved list.")
    assert (r.status, r.request_id) == ("PENDING", 5)
    assert "owner approval" in r.message and "payment_status(5)" in r.message and "Do not submit" in r.message


def test_failed_with_tx_tells_the_agent_not_to_pay_again():
    r = results.failed(8, "0xdead")
    assert r.status == "FAILED" and "Do not repay" in r.message and r.explorer_url and r.tx_hash == "0xdead"


def test_failed_without_tx_does_not_claim_money_moved_or_not():
    assert "payment_status(8)" in results.failed(8).message


def row(status, **extra):
    base = {"id": 3, "status": status, "url": "http://x/a", "pay_to": "0xSeller", "amount": 500_000,
            "decision": None, "rule_fired": None, "tx_hash": None, "reason": "r"}
    return {**base, **extra}


def test_attempt_rows_map_to_every_status():
    assert results.from_attempt(row("settled", tx_hash="0xabc"), "data").status == "SETTLED"
    assert results.from_attempt(row("settled", tx_hash="0xabc"), "data").body == "data"
    blocked = results.from_attempt(row("blocked", rule_fired="cap: Over the limit."), None)
    assert (blocked.status, blocked.rule) == ("BLOCKED", "cap") and "Over the limit." in blocked.message
    assert results.from_attempt(row("pending", rule_fired="seller: Not approved."), None).status == "PENDING"
    assert results.from_attempt(row("approved"), None).status == "PROCESSING"
    assert results.from_attempt(row("denied"), None).status == "DENIED"
    assert results.from_attempt(row("failed"), None).status == "FAILED"


def test_settled_attempt_with_no_stored_body_says_so():
    r = results.from_attempt(row("settled", tx_hash="0xabc"), None)
    assert r.status == "SETTLED" and r.body is None and "no longer held" in r.message


def test_reasons_without_a_final_period_still_read_as_sentences():
    assert results.sentence("is not an approved seller") == "is not an approved seller."
    assert results.sentence("Already done.") == "Already done."
    assert results.sentence("") == ""
    msg = results.pending(6, "seller", "0xabc is not an approved seller").message
    assert "approved seller. Check payment_status(6)" in msg
    assert "Blocked. Too high. Do not retry" in results.blocked(1, "cap", "Too high").message
