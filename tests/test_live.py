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
    assert states(paid) == {"found": "ok", "guard": "ok", "intent": "ok", "payment": "ok", "delivery": "ok",
                            "result": "ok"}
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


def test_an_escrow_waiting_for_the_owner_is_amber_with_a_tasks_hint():
    held = row(status="escrowed", escrow_state="authorized", escrow_held=True, delivery_verdict="unsure")
    assert states(held)["delivery"] == "wait" and states(held)["result"] == "wait"
    assert "Tasks" in step(held, "result")["note"]


@pytest.mark.parametrize("rule", sorted(pages.GUARD_RULES))
def test_a_guard_block_stops_at_policy_and_nothing_after_is_reached(rule):
    blocked = row(status="blocked", rule=rule, detail="Because.", tx_hash=None, intent_tier=None)
    got = states(blocked)
    assert got["guard"] == "stop"
    assert got["intent"] == got["payment"] == got["delivery"] == "idle" and got["result"] == "stop"
    assert "Because." in step(blocked, "guard")["note"]


def test_an_intent_block_passes_policy_and_stops_at_the_task_check():
    blocked = row(status="blocked", rule="injection", detail="Seller text addressed the agent.", tx_hash=None)
    got = states(blocked)
    assert got["guard"] == "ok" and got["intent"] == "stop" and got["payment"] == "idle"
    assert step(blocked, "intent")["note"].startswith("Seller tried to instruct the agent")


def test_a_judge_hold_waits_for_the_owner_and_pays_nothing():
    held = row(status="escalated", rule="judge", detail="Not clearly part of the task.", intent_tier=2, tx_hash=None)
    got = states(held)
    assert got["intent"] == "wait" and got["payment"] == "idle" and got["result"] == "wait"
    assert step(held, "intent")["note"].startswith("Held for you")


def test_the_tier_that_passed_is_named():
    assert step(row(intent_tier=2, intent_reason="Funding data fits."), "intent")["note"] == (
        "Judge: on task. Funding data fits.")
    assert step(row(intent_tier=0), "intent")["note"] == "Rules passed"


@pytest.mark.parametrize("status, state", [
    ("pending", "wait"), ("authorized", "ok"), ("expired", "stop"), ("cancelled", "stop"),
])
def test_an_approval_step_shows_only_for_approvals(status, state):
    payment = row(status=status, rule="approval", tx_hash=None)
    assert states(payment)["approval"] == state
    assert "approval" not in states(row())


def test_a_row_found_while_searching_says_so():
    searched = row(status="blocked", rule="bait_switch", found_while_searching=True, tx_hash=None)
    assert "searching" in step(searched, "found")["note"]
    assert states(searched)["intent"] == "stop"


def test_found_at_shows_where_the_agent_found_it():
    assert step(row(found_at="https://market.test/catalog?x=1"), "found")["note"].endswith("via market.test/catalog")
    assert step(row(found_at="sentinel discovery"), "found")["note"].endswith("via sentinel discovery")

