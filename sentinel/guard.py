"""Deterministic payment policy: ALLOW or BLOCK. Sentinel decides; nobody is asked per payment.

evaluate() is pure, so the same inputs always give the same decision. payments.py calls it before
anything is signed, and test_guard.py is its answer key. Rules run in order and the first to fire wins:
  1. long_window    the seller wants an authorization valid longer than max_payment_window_s, long enough to hold the
                    signature and settle it much later -> BLOCK
  2. lookalike      the payee is not approved but imitates an approved seller's (or the owner's own) address: same
                    first 6 and last 4 hex characters, the address poisoning trick -> BLOCK
  3. flagged_wallet the Base Sepolia explorer flags the payee as a scam (chain.wallet_profile) -> BLOCK
  4. seller         on the owner's blocked list -> BLOCK
  5. payee_changed  this agent already paid this exact URL to a different address: the payee was swapped -> BLOCK
  6. cap            amount above the agent's per payment cap -> BLOCK
  7. budget         rolling spend + amount above the weekly budget -> BLOCK
  8. price_jump     amount above 2x the median of the last 5 settled prices of this product (seller and URL) -> BLOCK
                    (no history means the first price is trusted and becomes the baseline)
  9. duplicate      same agent, payTo, url and amount inside the duplicate window -> BLOCK
 10. seller_escrow  a seller not on the approved list that offers escrow is paid through escrow -> ALLOW
     unlisted       not on the approved list and no escrow offer: paid directly, after every check above and the
                    intent review (wallet history, injection, judge) -> ALLOW
 11. clean          an approved seller and everything passed -> ALLOW
"""
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ALLOW, BLOCK = "ALLOW", "BLOCK"

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
    escrow: bool = False  # paid through escrow: refunded automatically when the delivery check fails


@dataclass(frozen=True)
class Agent:
    """The requesting agent's limits, both in USDC atomic units, and the owner's auto buy switch (intent.py's
    pricier_pick rule: on, the recommended best value of a price comparison is bought without asking)."""

    name: str
    per_payment_cap: int
    weekly_budget: int
    auto_buy_best_value: bool = False


@dataclass(frozen=True)
class History:
    """Ledger facts about this quote, gathered by attempts.load_history."""

    seller_status: str | None  # approved, blocked, or None if the seller is not listed
    week_spend: int  # settled plus in flight spend inside the budget window
    seller_settled: tuple[int, ...] = ()  # this product's (seller and URL) most recent settled amounts, newest first
    duplicate: bool = False  # a matching attempt exists inside the duplicate window
    duplicate_of: int | None = None  # id of that earlier attempt, so the caller can be pointed at it
    lookalike_of: str | None = None  # the approved (or own) address an unapproved payee imitates, if any
    url_paid_to: tuple[str, ...] = ()  # addresses this agent already paid for this exact URL
    payee_flagged: bool = False  # the explorer flags the payee as a scam (payments.payee_profile)
    wallet_summary: str | None = None  # the payee's history in a few words (chain.WalletProfile.summary)


@dataclass(frozen=True)
class Decision:
    """The guard's verdict: ALLOW or BLOCK, which rule decided, and a plain English reason.

    related_id is the earlier attempt a duplicate block refers to, if any.
    """

    decision: str
    rule: str
    detail: str
    related_id: int | None = None
    ml: "MLScore | None" = None  # the model score behind this decision, if a model decided it


@dataclass(frozen=True)
class MLScore:
    """One model's score for a payment: model is 'injection', score and threshold are 0 to 1."""

    model: str
    score: float
    threshold: float

    def columns(self) -> dict[str, Any]:
        """The attempts.ML_COLUMNS values."""
        return {"ml_model": self.model, "ml_score": self.score, "ml_threshold": self.threshold}


def imitates(address: str, known: list[str]) -> str | None:
    """The known address this one imitates (same first 6 and last 4 hex characters, not the same address), or None.

    Address poisoning sends a payment to a wallet that looks right at a glance in a shortened 0x1234ab...cdef view.
    """
    a = address.lower()
    if len(a) != 42:
        return None
    for candidate in known:
        c = candidate.lower()
        if len(c) == 42 and c != a and c[2:8] == a[2:8] and c[-4:] == a[-4:]:
            return candidate
    return None


def usd(atomic: int) -> str:
    """Format USDC atomic units as dollars for messages only, never rounded: $0.50, $10.00, $0.001, $1.001."""
    text = format((Decimal(atomic) / 1_000_000).normalize(), "f")
    whole, _, frac = text.partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"


def evaluate(agent: Agent, quote: Quote, history: History, policy: dict[str, int] = POLICY) -> Decision:
    """Sentinel's policy decision for this quote: ALLOW or BLOCK, never a question for a human.

    detail is written for the owner and for the agent that asked, so it avoids jargon and rule names.
    """
    if quote.timeout_s > policy["max_payment_window_s"]:
        return Decision(
            BLOCK, "long_window",
            f"The seller wants the payment authorization to stay valid for {quote.timeout_s // 60} minutes, long "
            f"enough to hold it and cash it later. Sentinel signs for at most "
            f"{policy['max_payment_window_s'] // 60} minutes.",
        )

    if history.lookalike_of is not None and history.seller_status != "approved":
        return Decision(
            BLOCK, "lookalike",
            f"The seller's address {quote.pay_to} imitates {history.lookalike_of} (same start and end, different "
            "middle). That is address poisoning: someone hoping a payment goes to the wrong wallet.",
        )

    if history.payee_flagged:
        return Decision(
            BLOCK, "flagged_wallet",
            f"The Base Sepolia explorer flags the seller's wallet {quote.pay_to} as a scam.",
        )

    if history.seller_status == "blocked":
        return Decision(BLOCK, "seller", f"The seller {quote.pay_to} is on the owner's blocked list.")

    paid_before = {address.lower() for address in history.url_paid_to}
    if paid_before and quote.pay_to.lower() not in paid_before:
        earlier = ", ".join(sorted(history.url_paid_to))
        return Decision(
            BLOCK, "payee_changed",
            f"This URL was paid to {earlier} before, but now asks to be paid to {quote.pay_to}. The payee was swapped.",
        )

    if quote.amount > agent.per_payment_cap:
        return Decision(
            BLOCK, "cap",
            f"{usd(quote.amount)} is over this agent's limit of {usd(agent.per_payment_cap)} per payment.",
        )

    if history.week_spend + quote.amount > agent.weekly_budget:
        days = policy["budget_window_days"]
        return Decision(
            BLOCK, "budget",
            f"{usd(history.week_spend)} was already spent in the last {days} days, so adding {usd(quote.amount)} "
            f"would go over the {usd(agent.weekly_budget)} budget.",
        )

    recent = sorted(history.seller_settled[: policy["price_jump_window"]])
    if recent:
        # Twice the median, so the comparison stays in integers even when the median falls between two prices.
        twice_median = recent[len(recent) // 2] + recent[(len(recent) - 1) // 2]
        if quote.amount * 2 > policy["price_jump_multiplier"] * twice_median:
            ratio = quote.amount * 2 / twice_median  # display only
            return Decision(
                BLOCK, "price_jump",
                f"This product's price is {ratio:.1f}x its usual ({usd(quote.amount)} vs a typical "
                f"{usd(twice_median // 2)}). More than {policy['price_jump_multiplier']}x is not allowed.",
            )

    if history.duplicate:
        minutes = policy["duplicate_window_s"] // 60
        earlier = f" (request {history.duplicate_of})" if history.duplicate_of else ""
        return Decision(
            BLOCK, "duplicate",
            f"This exact payment (same seller, URL and amount) was already made or is in progress{earlier} "
            f"within the last {minutes} minutes.",
            related_id=history.duplicate_of,
        )

    if history.seller_status != "approved":
        return judge_seller(quote, history)
    return Decision(ALLOW, "clean", "All checks passed.")


def judge_seller(quote: Quote, history: History) -> Decision:
    """Rule 10: a seller not on the approved list is paid through escrow when it offers it, else directly."""
    facts = f" ({history.wallet_summary})" if history.wallet_summary else ""
    if quote.escrow:
        return Decision(
            ALLOW, "seller_escrow",
            f"This seller is not on your approved list{facts}. Paying through escrow, so the money comes back if the "
            "delivery fails the check.",
        )
    return Decision(
        ALLOW, "unlisted",
        f"This seller is not on your approved list{facts} and does not offer escrow, so it is paid directly once "
        "every other check passes.",
    )
