"""Answer key for the guard. Each planted case maps to one rule; the rest are edge cases."""
import pytest

from sentinel.guard import ALLOW, BLOCK, Agent, History, Quote, evaluate, imitates

USDC = 1_000_000
DATACO = "0x290E84b17Ce32cF9403f5D0218cB50ccdcc94781"
SCAMMER = "0xSca000000000000000000000000000000000dead"
AGENT = Agent("research-agent", per_payment_cap=10 * USDC, weekly_budget=50 * USDC)


def quote(amount: int, pay_to: str = DATACO) -> Quote:
    return Quote("http://x/a", pay_to, amount, "0xusdc", "eip155:84532", 300)


def history(**overrides) -> History:
    base = dict(seller_status="approved", week_spend=0, seller_settled=(), duplicate=False)
    return History(**{**base, **overrides})


def test_1_seller_unknown_scammer_blocks():
    d = evaluate(AGENT, quote(9 * USDC, SCAMMER), history(seller_status=None))
    assert (d.decision, d.rule) == (BLOCK, "seller")


def test_2_cap_15_vs_10_blocks():
    d = evaluate(AGENT, quote(15 * USDC), history())
    assert (d.decision, d.rule) == (BLOCK, "cap")


def test_3_budget_49_80_spent_plus_0_50_blocks():
    d = evaluate(AGENT, quote(500_000), history(week_spend=49_800_000))
    assert (d.decision, d.rule) == (BLOCK, "budget")


def test_4_price_jump_2_00_vs_0_50_history_blocks():
    d = evaluate(AGENT, quote(2 * USDC), history(seller_settled=(500_000,) * 5))
    assert (d.decision, d.rule) == (BLOCK, "price_jump")


def test_5_duplicate_repeat_of_settled_call_blocks():
    d = evaluate(AGENT, quote(500_000), history(seller_settled=(500_000,), duplicate=True))
    assert (d.decision, d.rule) == (BLOCK, "duplicate")


def test_6_clean_dataco_0_50_allows():
    d = evaluate(AGENT, quote(500_000), history(seller_settled=(500_000,) * 3))
    assert (d.decision, d.rule) == (ALLOW, "clean")


def test_price_jump_exactly_2x_is_allowed():
    assert evaluate(AGENT, quote(USDC), history(seller_settled=(500_000,) * 5)).decision == ALLOW


def test_first_purchase_has_no_history_and_is_trusted():
    assert evaluate(AGENT, quote(5 * USDC), history(seller_settled=())).decision == ALLOW


def test_blocked_seller_is_blocked():
    d = evaluate(AGENT, quote(500_000), history(seller_status="blocked"))
    assert (d.decision, d.rule) == (BLOCK, "seller") and "blocked list" in d.detail


def test_pending_seller_is_not_approved_so_it_blocks():
    # Older rows may still say pending; only "approved" lets money move.
    d = evaluate(AGENT, quote(500_000), history(seller_status="pending"))
    assert (d.decision, d.rule) == (BLOCK, "seller")


def test_seller_rule_runs_before_cap():
    # A $15 quote from an unknown seller is reported as a seller problem, not a cap problem.
    assert evaluate(AGENT, quote(15 * USDC, SCAMMER), history(seller_status=None)).rule == "seller"


def test_amount_exactly_at_cap_and_budget_is_allowed():
    d = evaluate(AGENT, quote(10 * USDC), history(week_spend=40 * USDC))
    assert d.decision == ALLOW


def test_median_uses_middle_of_even_history():
    # history 0.40 and 0.60 has median 0.50, so 1.00 is exactly 2x and passes, 1.01 does not.
    assert evaluate(AGENT, quote(USDC), history(seller_settled=(400_000, 600_000))).decision == ALLOW
    assert evaluate(AGENT, quote(1_010_000), history(seller_settled=(400_000, 600_000))).rule == "price_jump"


@pytest.mark.parametrize("amount", [1, 499_999])
def test_cheaper_than_history_is_fine(amount):
    assert evaluate(AGENT, quote(amount), history(seller_settled=(500_000,) * 5)).decision == ALLOW


# Messages are shown to the owner and to the agent, so they must be plain English with the numbers in them.

def test_price_jump_message_states_the_multiple():
    d = evaluate(AGENT, quote(2 * USDC), history(seller_settled=(500_000,) * 5))
    assert "4.0x" in d.detail and "$2.00" in d.detail and "$0.50" in d.detail


def test_cap_budget_and_seller_messages_name_the_numbers():
    assert "$15.00" in evaluate(AGENT, quote(15 * USDC), history()).detail
    assert "$10.00" in evaluate(AGENT, quote(15 * USDC), history()).detail
    assert "$49.80" in evaluate(AGENT, quote(500_000), history(week_spend=49_800_000)).detail
    assert SCAMMER in evaluate(AGENT, quote(USDC, SCAMMER), history(seller_status=None)).detail


def test_duplicate_message_points_at_the_earlier_request():
    d = evaluate(AGENT, quote(500_000), history(duplicate=True, duplicate_of=7))
    assert d.related_id == 7 and "request 7" in d.detail
    assert evaluate(AGENT, quote(500_000), history(duplicate=True)).related_id is None


# Seller attacks


LOOKALIKE = "0x290E84" + "0" * 30 + "4781"  # same first 6 and last 4 hex as DataCo


def test_a_payment_window_longer_than_ten_minutes_blocks():
    long = Quote("http://x/a", DATACO, 500_000, "0xusdc", "eip155:84532", 24 * 3600)
    d = evaluate(AGENT, long, history())
    assert (d.decision, d.rule) == (BLOCK, "long_window") and "1440 minutes" in d.detail
    assert evaluate(AGENT, Quote("http://x/a", DATACO, 500_000, "0xusdc", "eip155:84532", 600), history()).decision \
        == ALLOW


def test_a_lookalike_of_an_approved_address_blocks_and_names_what_it_imitates():
    d = evaluate(AGENT, quote(500_000, LOOKALIKE), history(seller_status=None, lookalike_of=DATACO))
    assert (d.decision, d.rule) == (BLOCK, "lookalike") and DATACO in d.detail and LOOKALIKE in d.detail


def test_an_approved_address_is_never_called_a_lookalike():
    assert evaluate(AGENT, quote(500_000), history(lookalike_of="0xsomething")).decision == ALLOW


def test_imitates_needs_the_same_start_and_end_but_a_different_address():
    assert imitates(LOOKALIKE, [DATACO]) == DATACO
    assert imitates(LOOKALIKE.upper().replace("0X", "0x"), [DATACO]) == DATACO
    assert imitates(DATACO, [DATACO]) is None  # the same address
    assert imitates("0x290E85" + "0" * 30 + "4781", [DATACO]) is None  # start differs
    assert imitates("0x290E84" + "0" * 30 + "4782", [DATACO]) is None  # end differs
    assert imitates("0x1234", [DATACO]) is None


def test_a_url_paid_to_one_wallet_that_now_asks_for_another_blocks():
    d = evaluate(AGENT, quote(500_000), history(url_paid_to=("0xEarlier",)))
    assert (d.decision, d.rule) == (BLOCK, "payee_changed") and "0xEarlier" in d.detail
    assert evaluate(AGENT, quote(500_000), history(url_paid_to=(DATACO.lower(),))).decision == ALLOW


def test_seller_attacks_are_checked_first():
    long = Quote("http://x/a", LOOKALIKE, 15 * USDC, "0xusdc", "eip155:84532", 3600)
    assert evaluate(AGENT, long, history(seller_status=None, lookalike_of=DATACO)).rule == "long_window"
    assert evaluate(AGENT, quote(15 * USDC, LOOKALIKE), history(seller_status=None, lookalike_of=DATACO)).rule \
        == "lookalike"
    assert evaluate(AGENT, quote(15 * USDC), history(url_paid_to=("0xEarlier",))).rule == "payee_changed"


def test_a_payee_the_explorer_flags_as_a_scam_is_blocked_even_when_approved():
    d = evaluate(AGENT, quote(500_000), history(payee_flagged=True))
    assert (d.decision, d.rule) == (BLOCK, "flagged_wallet") and DATACO in d.detail
