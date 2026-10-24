"""Deterministic payment policy: decides ALLOW, BLOCK or NEEDS_APPROVAL for one quote.

evaluate() is pure, so the same inputs always give the same decision. payments.py calls it before
anything is signed, and test_guard.py is its answer key. Rules run in order and the first to fire wins:
  1. seller      unlisted or pending seller -> NEEDS_APPROVAL, blocked seller -> BLOCK
  2. cap         amount above the agent's per payment cap -> BLOCK
  3. budget      rolling spend + amount above the weekly budget -> BLOCK
  4. price_jump  amount above 2x the median of the seller's last 5 settled -> BLOCK
                 (no history means the first price is trusted and becomes the baseline)
  5. duplicate   same agent, payTo, url and amount inside the duplicate window -> BLOCK
  6. clean       everything passed -> ALLOW
"""
import json
from dataclasses import dataclass
from pathlib import Path

ALLOW, BLOCK, NEEDS_APPROVAL = "ALLOW", "BLOCK", "NEEDS_APPROVAL"

POLICY: dict[str, int] = json.loads((Path(__file__).parent / "policy.json").read_text())


@dataclass(frozen=True)
class Quote:
    """What the seller is asking for. Amount is in USDC atomic units (6 decimals); quotes expire after timeout_s."""

    url: str
    pay_to: str
    amount: int
    asset: str
    network: str
    timeout_s: int


@dataclass(frozen=True)
class Agent:
    """The requesting agent's limits, both in USDC atomic units."""

    name: str
    per_payment_cap: int
    weekly_budget: int


@dataclass(frozen=True)
class History:
    """Ledger facts about this quote, gathered by attempts.load_history."""

    seller_status: str | None  # approved, pending, blocked, or None if the seller is not listed
    week_spend: int  # settled plus in flight spend inside the budget window
    seller_settled: tuple[int, ...] = ()  # this seller's most recent settled amounts, newest first
    duplicate: bool = False  # a matching attempt exists inside the duplicate window


@dataclass(frozen=True)
class Decision:
    """The guard's verdict: ALLOW, BLOCK or NEEDS_APPROVAL, which rule decided, and a readable reason."""

    decision: str
    rule: str
    detail: str


def usd(atomic: int) -> str:
    """Format USDC atomic units as dollars for messages only. Never used for arithmetic."""
    return f"${atomic / 1_000_000:.2f}"


def evaluate(agent: Agent, quote: Quote, history: History, policy: dict[str, int] = POLICY) -> Decision:
    """Return the decision for this agent paying this quote, given what the ledger already holds."""
    if history.seller_status == "blocked":
        return Decision(BLOCK, "seller", f"{quote.pay_to} is on the blocked list")
    # Unknown sellers pause for a human instead of blocking, so a new legitimate seller can be approved.
    if history.seller_status != "approved":
        return Decision(NEEDS_APPROVAL, "seller", f"{quote.pay_to} is not an approved seller")

    if quote.amount > agent.per_payment_cap:
        return Decision(BLOCK, "cap", f"{usd(quote.amount)} exceeds per payment cap {usd(agent.per_payment_cap)}")

    if history.week_spend + quote.amount > agent.weekly_budget:
        return Decision(
            BLOCK, "budget",
            f"{usd(history.week_spend)} spent + {usd(quote.amount)} exceeds weekly budget {usd(agent.weekly_budget)}",
        )

    recent = sorted(history.seller_settled[: policy["price_jump_window"]])
    if recent:
        # Twice the median, so the comparison stays in integers even when the median falls between two prices.
        twice_median = recent[len(recent) // 2] + recent[(len(recent) - 1) // 2]
        if quote.amount * 2 > policy["price_jump_multiplier"] * twice_median:
            return Decision(
                BLOCK, "price_jump",
                f"{usd(quote.amount)} is more than {policy['price_jump_multiplier']}x the median "
                f"{usd(twice_median // 2)} of the last {len(recent)} settled",
            )

    if history.duplicate:
        minutes = policy["duplicate_window_s"] // 60
        return Decision(BLOCK, "duplicate", f"same payTo, url and amount already attempted in the last {minutes} min")

    return Decision(ALLOW, "clean", "all rules passed")
