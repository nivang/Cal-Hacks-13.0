"""Tests for the Live page's stepper (web/pages.pipeline): which step stopped a payment and what each step says.

Pure: each case is one payment row the way owner.list_attempts returns it. No database.
"""
import pytest

from sentinel.web import pages


def row(**kw):
    base = {"status": "settled", "rule": "clean", "detail": "All checks passed.", "intent_tier": 0,
            "tx_hash": "0xpaid", "found_while_searching": False}
    return {**base, **kw}


def states(payment):
    return {s["key"]: s["state"] for s in pages.pipeline(payment)}


def step(payment, key):
    return next(s for s in pages.pipeline(payment) if s["key"] == key)


def test_a_direct_payment_passes_every_step_and_links_its_transaction():
    paid = row(delivery_verdict="pass")
    assert states(paid) == {"found": "ok", "guard": "ok", "seller": "ok", "intent": "ok", "payment": "ok",
                            "delivery": "ok", "result": "ok"}
    assert step(paid, "result")["tx"] == "0xpaid"
    assert "no escrow" in step(paid, "payment")["note"]


def test_an_escrow_capture_links_the_hold_and_the_capture():
    captured = row(escrow_state="captured", authorize_tx="0xhold", capture_tx="0xcapture", delivery_verdict="pass")
    assert step(captured, "payment")["tx"] == "0xhold"
    result = step(captured, "result")
    assert result["state"] == "ok" and result["tx"] == "0xcapture"


def test_a_voided_escrow_stops_at_delivery_and_says_refunded():
    voided = row(status="voided", escrow_state="voided", authorize_tx="0xhold", void_tx="0xvoid",
                 delivery_verdict="fail", delivery_reason="empty: The seller sent nothing.")
    assert states(voided)["delivery"] == "stop"
    assert step(voided, "delivery")["note"] == "The seller sent nothing."
    assert step(voided, "result")["tx"] == "0xvoid" and "Refunded" in step(voided, "result")["note"]


def test_an_escrow_in_flight_is_sentinel_checking_and_never_asks_the_owner():
    held = row(status="escrowed", escrow_state="authorized", escrow_held=True, delivery_verdict=None)
    assert states(held)["delivery"] == "wait" and states(held)["result"] == "wait"
    notes = " ".join(s["note"] for s in pages.pipeline(held)).lower()
    assert "waiting" not in notes and "tasks" not in notes


@pytest.mark.parametrize("rule", sorted(pages.GUARD_RULES - pages.SELLER_RULES))
def test_a_guard_block_stops_at_policy_and_nothing_after_is_reached(rule):
    blocked = row(status="blocked", rule=rule, detail="Because.", tx_hash=None, intent_tier=None)
    got = states(blocked)
    assert got["guard"] == "stop"
    assert got["seller"] == got["intent"] == got["payment"] == got["delivery"] == "idle" and got["result"] == "stop"
    assert "Because." in step(blocked, "guard")["note"]


def test_the_wallet_model_denying_a_seller_stops_at_the_seller_step_with_its_score():
    denied = row(status="blocked", rule="wallet_risk", detail="Scored 0.97.", tx_hash=None, intent_tier=None,
                 ml_model="wallet_risk", ml_score=0.97, ml_threshold=0.5)
    got = states(denied)
    assert got["guard"] == "ok" and got["seller"] == "stop" and got["intent"] == "idle"
    seller = step(denied, "seller")
    assert "Wallet risk model 0.97 · escrow only at 0.50" in seller["note"] and seller["ml"]["hit"] is True


def test_the_wallet_model_clearing_a_seller_names_its_score():
    paid = row(seller=None, ml_model="wallet_risk", ml_score=0.12, ml_threshold=0.5, rule="wallet_risk_ok")
    seller = step(paid, "seller")
    assert seller["state"] == "ok" and seller["ml"]["hit"] is False
    assert "Wallet risk model 0.12 · escrow only at 0.50" in seller["note"]


def test_an_unreadable_wallet_stops_at_the_seller_step():
    got = states(row(status="blocked", rule="wallet_unknown", detail="No history.", tx_hash=None))
    assert got["seller"] == "stop" and got["guard"] == "ok"


def test_an_approved_seller_says_so():
    assert step(row(seller="CorpData"), "seller")["note"] == "On your approved list"


def test_an_intent_block_passes_policy_and_stops_at_the_task_check():
    blocked = row(status="blocked", rule="injection", detail="Seller text addressed the agent.", tx_hash=None)
    got = states(blocked)
    assert got["guard"] == "ok" and got["intent"] == "stop" and got["payment"] == "idle"
    assert step(blocked, "intent")["note"].startswith("Seller tried to instruct the agent")


def test_the_injection_model_names_its_score_at_the_task_check():
    blocked = row(status="blocked", rule="injection_ml", detail="Seller text.", tx_hash=None,
                  ml_model="injection", ml_score=0.97, ml_threshold=0.42)
    intent = step(blocked, "intent")
    assert intent["state"] == "stop" and intent["ml"]["hit"] is True
    assert intent["note"].startswith("Possible instructions in seller text")
    assert "Injection model 0.97 · blocks at 0.42" in intent["note"]


def test_an_old_escalated_row_reads_as_stopped_not_waiting():
    held = row(status="escalated", rule="judge", detail="Not clearly part of the task.", intent_tier=2, tx_hash=None)
    got = states(held)
    assert got["intent"] == "stop" and got["payment"] == "idle" and got["result"] == "stop"
    assert "wait" not in got.values()


def test_the_tier_that_passed_is_named():
    assert step(row(intent_tier=2, intent_reason="Funding data fits."), "intent")["note"] == (
        "Judge: on task. Funding data fits.")
    assert step(row(intent_tier=0), "intent")["note"] == "Rules passed"


@pytest.mark.parametrize("status", ["pending", "authorized", "denied", "expired", "cancelled"])
def test_old_approval_rows_stop_at_the_seller_step_and_never_wait(status):
    got = states(row(status=status, rule="approval", tx_hash=None))
    assert got["seller"] == "stop" and got["result"] == "stop" and "wait" not in got.values()
    assert "approval" not in got


def test_ml_badge_text_and_colour():
    assert pages.ml_badge(row()) is None
    badge = pages.ml_badge(row(ml_model="injection", ml_score=0.03, ml_threshold=0.42))
    assert badge == {"model": "injection", "text": "Injection model 0.03 · blocks at 0.42", "hit": False}


def test_a_row_found_while_searching_says_so():
    searched = row(status="blocked", rule="bait_switch", found_while_searching=True, tx_hash=None)
    assert "searching" in step(searched, "found")["note"]
    assert states(searched)["intent"] == "stop"


def test_found_at_shows_where_the_agent_found_it():
    assert step(row(found_at="https://market.test/catalog?x=1"), "found")["note"].endswith("via market.test/catalog")
    assert step(row(found_at="sentinel discovery"), "found")["note"].endswith("via sentinel discovery")

