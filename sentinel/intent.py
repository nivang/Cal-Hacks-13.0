"""Intent binding: does this payment make sense for the task the owner gave the agent?

guard.py asks fixed questions (approved seller, cap, budget, price, duplicate). This module asks whether a payment
fits the mandate, the structured form of the owner's task (mandates.py stores it, judge.py writes it from the owner's
words). It only runs after the guard says ALLOW, and it can only hold or block: the approved seller list stays the
hard rule. Decisions come in tiers, fast to smart, and the first to decide wins:
  tier 0  rules on the mandate and the session, here, pure (about 1 ms):
            1. no_mandate      the task was revoked or has expired -> BLOCK
            seller attacks, checked against what the seller's own text says and what the catalog advertised:
            a. injection       seller text addresses AI agents or claims the payment is authorized -> BLOCK
            b. unlisted_url    the agent says it found the URL through Sentinel's catalog, but it was never listed for
                               this task (made up, or fed to the agent) -> BLOCK
            c. bait_switch     the paywall asks more than the listing advertised, or pays a different wallet -> BLOCK
            2. mandate_budget  spend under this task + amount above its budget (the provisional cap until the owner
                               confirms the task) -> BLOCK
            3. rate            too many payments under this task in the last minute -> BLOCK
            4. loop            this URL was already bought intent_loop_count times under this task -> BLOCK
            5. allowance       the owner allowed this exact held payment (Allow once) -> ALLOW
            5b. unused_wallet  the payee's wallet has no activity at all on Base Sepolia -> ESCALATE
            6. split           this seller's spend in the split window + amount above the per payment cap, so one
                               purchase may be cut into small ones -> ESCALATE
            7. per_call        amount above intent_per_call_multiple x the task's per call estimate -> ESCALATE
            8. big_share       one purchase above intent_big_share_percent of the task's budget -> ESCALATE
            9. drift           most recent decisions under this task needed the judge -> ESCALATE
            10. listing_changed the paywall describes a different product than the listing did -> ESCALATE
            11. pricier_pick   a price comparison under this task offered this URL and the job costs more than the
                               cheapest buyable option it found: held, unless it is the recommended best value and
                               the owner turned on auto buy -> ESCALATE
  tier 1  a fast model check of the seller's description against the mandate (judge.classify), cached per task,
          seller and URL: fits -> ALLOW, off_task -> BLOCK, unsure -> tier 2. A link the agent found on a web page
          that the owner never named cannot be allowed here; it goes to tier 2.
  tier 2  the judge (judge.decide) sees the mandate, the payment, the agent's reason and this task's history, and
          answers allow, block or escalate.
payments.review_intent runs the tiers; everything it needs from the database arrives here as plain dataclasses, so
the same inputs always give the same decision and test_intent.py is the answer key for tier 0.
Seller text and the agent's reason are untrusted: case() hands them to the judge as quoted data, never instructions.
"""
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sentinel import guard

ALLOW, BLOCK, ESCALATE = guard.ALLOW, guard.BLOCK, "ESCALATE"
FITS, OFF_TASK, UNSURE = "fits", "off_task", "unsure"
PROVISIONAL, CONFIRMED, REVOKED = "provisional", "confirmed", "revoked"
MAX_SELLER_TEXT = 500  # characters of seller supplied text kept per field
MAX_HISTORY = 10  # earlier purchases the judge sees
DISCOVERY = "sentinel discovery"  # the found_at of a URL the agent took from find_services
# Seller text that talks to the agent instead of describing a product: a prompt injection attempt.
INJECTION = re.compile(
    r"\bai agents?\b|\b(?:llms?|language models?|assistants?)\s*[:,]|pre-?authori[sz]ed|"
    r"authori[sz]ed by (?:your|the) (?:owner|user)|\bpay (?:now|immediately)\b|ignore (?:all |any |your |previous )|"
    r"without asking|do not (?:ask|tell|check with) (?:the|your) (?:user|owner)|system prompt|"
    r"you (?:must|should) (?:pay|buy|purchase)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Mandate:
    """What spending the owner's task allows. Amounts are USDC atomic units."""

    id: int
    task_text: str  # the owner's own words, as the agent quoted them
    goal: str
    expected_types: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    budget: int
    per_call: int | None  # a typical price per purchase, when the task implies one
    status: str  # provisional, confirmed or revoked
    expired: bool


@dataclass(frozen=True)
class Seller:
    """What the seller says it sells, from its 402 response. Written by the seller, so it is data, never trusted."""

    description: str | None = None
    service_name: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Payment:
    """One payment under review: the quote, the agent's reason, where the agent says it found the link."""

    quote: guard.Quote
    reason: str
    found_at: str | None
    seller: Seller


@dataclass(frozen=True)
class Purchase:
    """An earlier payment under the same task, for the judge."""

    url: str
    amount: int
    status: str
    reason: str | None


@dataclass(frozen=True)
class Listing:
    """What the catalog advertised for this URL when the agent searched under this task (mandates.listings_seen)."""

    amount: int | None
    pay_to: str | None
    name: str | None
    description: str | None


@dataclass(frozen=True)
class Comparison:
    """The latest price comparison under this task that offered this URL (mandates.comparison_for). Atomic units."""

    id: int
    best_key: str | None  # the recommended best value: URL without its query
    best_name: str | None
    cheapest_key: str | None  # the cheapest offer Sentinel could buy and recommend
    cheapest_name: str | None
    cheapest_cost: int | None  # what the job costs with it
    calls: int  # the calls the comparison priced the job at
    explanation: str


@dataclass(frozen=True)
class Session:
    """Facts about this task so far, gathered by mandates.session_facts. Amounts are atomic units."""

    spend: int  # settled plus in flight under this task
    last_minute: int  # payment attempts under this task in the last minute, any outcome
    url_bought: int  # settled or in flight purchases of this URL under this task
    seller_window_spend: int  # settled plus in flight paid to this seller in the split window, any task
    recent_tiers: tuple[int, ...] = ()  # intent tiers of the latest decisions under this task, newest first
    allowance_id: int | None = None  # an unused Allow once matching this payment
    purchases: tuple[Purchase, ...] = ()  # newest first
    listing: Listing | None = None  # what the catalog advertised for this URL under this task, if it was listed
    payee_unused: bool = False  # the payee's wallet never sent or received anything (payments.payee_profile)
    comparison: Comparison | None = None  # the latest price comparison under this task that offered this URL


@dataclass(frozen=True)
class Verdict:
    """The intent decision: ALLOW, BLOCK or ESCALATE, which tier and rule decided, and a plain English reason."""

    decision: str
    tier: int
    rule: str
    detail: str
    allowance_id: int | None = None


def effective_budget(mandate: Mandate, policy: dict[str, int] = guard.POLICY) -> int:
    """The task's budget, held to the provisional cap until the owner confirms the task."""
    if mandate.status == CONFIRMED:
        return mandate.budget
    return min(mandate.budget, policy["intent_provisional_budget"])


def tier0(
    mandate: Mandate, payment: Payment, session: Session, agent: guard.Agent, policy: dict[str, int] = guard.POLICY,
) -> Verdict | None:
    """The rules, in order; the first to fire decides. None means none fired and the model tiers decide.

    detail is written for the owner and for the agent, so it avoids rule names.
    """
    amount = payment.quote.amount
    if mandate.status == REVOKED:
        return Verdict(BLOCK, 0, "no_mandate", "The owner revoked this task, so nothing more can be bought for it.")
    if mandate.expired:
        return Verdict(BLOCK, 0, "no_mandate", "This task has expired. Start a new task with the owner's words.")

    seller_text = " ".join(filter(None, (payment.seller.description, payment.seller.service_name,
                                         *payment.seller.tags)))
    listing = session.listing
    if listing is not None:
        seller_text += " " + " ".join(filter(None, (listing.name, listing.description)))
    attack = INJECTION.search(seller_text)
    if attack:
        return Verdict(
            BLOCK, 0, "injection",
            f"The seller's own text talks to AI agents (\"{attack.group(0)}\") instead of describing what it sells. "
            "That is a prompt injection attempt.",
        )

    if listing is None and (payment.found_at or "").strip().lower() == DISCOVERY:
        return Verdict(
            BLOCK, 0, "unlisted_url",
            "The agent said it found this URL in Sentinel's catalog, but the catalog never listed it for this task. "
            "It was made up or fed to the agent.",
        )
    if listing is not None and listing.amount is not None and amount > listing.amount:
        return Verdict(
            BLOCK, 0, "bait_switch",
            f"The listing advertised {guard.usd(listing.amount)}, but the seller now asks {guard.usd(amount)}.",
        )
    if listing is not None and listing.pay_to and listing.pay_to.lower() != payment.quote.pay_to.lower():
        return Verdict(
            BLOCK, 0, "bait_switch",
            f"The listing said payments go to {listing.pay_to}, but the seller now asks to be paid at "
            f"{payment.quote.pay_to}.",
        )

    budget = effective_budget(mandate, policy)
    if session.spend + amount > budget:
        if mandate.status == CONFIRMED:
            why = f"the task's budget of {guard.usd(budget)}"
        else:
            why = (f"the {guard.usd(budget)} a task may spend before the owner confirms it on their Sentinel account "
                   "page")
        return Verdict(
            BLOCK, 0, "mandate_budget",
            f"{guard.usd(session.spend)} was already spent on this task, so adding {guard.usd(amount)} would go "
            f"over {why}.",
        )

    if session.last_minute >= policy["intent_rate_per_minute"]:
        return Verdict(
            BLOCK, 0, "rate",
            f"This task already made {session.last_minute} payment requests in the last minute. Slow down.",
        )

    if session.url_bought >= policy["intent_loop_count"]:
        return Verdict(
            BLOCK, 0, "loop",
            f"This URL was already bought {session.url_bought} times for this task. Reuse the data with "
            "payment_status instead of buying it again.",
        )

    if session.allowance_id is not None:
        return Verdict(ALLOW, 0, "allowance", "The owner allowed this payment.", session.allowance_id)

    if session.payee_unused:
        return Verdict(
            ESCALATE, 0, "unused_wallet",
            f"The seller's wallet {payment.quote.pay_to} has no activity at all on Base Sepolia. A real seller "
            "usually has a history, so the owner decides.",
        )

    if session.seller_window_spend + amount > agent.per_payment_cap:
        minutes = policy["intent_split_window_s"] // 60
        return Verdict(
            ESCALATE, 0, "split",
            f"Paying this seller {guard.usd(amount)} more would make {guard.usd(session.seller_window_spend + amount)}"
            f" in {minutes} minutes, over the {guard.usd(agent.per_payment_cap)} per payment cap. That can be one "
            "purchase cut into small ones, so the owner decides.",
        )

    multiple = policy["intent_per_call_multiple"]
    if mandate.per_call is not None and amount > multiple * mandate.per_call:
        return Verdict(
            ESCALATE, 0, "per_call",
            f"{guard.usd(amount)} is more than {multiple}x the {guard.usd(mandate.per_call)} this task expects per "
            "purchase, so the owner decides.",
        )

    share = policy["intent_big_share_percent"]
    if amount * 100 > share * mandate.budget:  # the task's real budget, not the cap before it is confirmed
        return Verdict(
            ESCALATE, 0, "big_share",
            f"{guard.usd(amount)} is more than {share}% of this task's {guard.usd(mandate.budget)} budget in one "
            "purchase, so the owner decides.",
        )

    window = session.recent_tiers[: policy["intent_drift_window"]]
    judged = sum(1 for tier in window if tier == 2)
    if len(window) >= policy["intent_drift_window"] and judged >= policy["intent_drift_judged"]:
        return Verdict(
            ESCALATE, 0, "drift",
            f"{judged} of this task's last {len(window)} purchases were not clearly part of it. The purchases may be "
            "drifting away from the goal, so the owner decides.",
        )

    if listing is not None and _differs(listing.description, payment.seller.description):
        return Verdict(
            ESCALATE, 0, "listing_changed",
            f"The catalog listed \"{clip(listing.description, 120)}\", but the paywall now sells "
            f"\"{clip(payment.seller.description, 120)}\". The owner decides.",
        )
    return pricier_pick(payment, session, agent)


def url_key(url: str) -> str:
    """A URL without its query and fragment: the product a price comparison offered, whatever inputs the agent sets."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return parts._replace(query="", fragment="").geturl()


def pricier_pick(payment: Payment, session: Session, agent: guard.Agent) -> Verdict | None:
    """Rule 11: a purchase that costs more than the cheapest buyable option its price comparison found is held.

    The recommended best value goes through without asking only when the owner turned on auto buy. The job is priced
    at the comparison's calls, so a purchase is compared like for like with the cheapest option.
    """
    pick = session.comparison
    if pick is None or pick.cheapest_cost is None or pick.cheapest_key is None:
        return None
    key = url_key(payment.quote.url)
    job = payment.quote.amount * max(1, pick.calls)
    if key == pick.cheapest_key or job <= pick.cheapest_cost:
        return None
    if key == pick.best_key:
        if agent.auto_buy_best_value:
            return None
        return Verdict(
            ESCALATE, 0, "pricier_pick",
            f"Sentinel's price comparison picked this as the best value, but at {guard.usd(job)} for the job it costs "
            f"more than the cheapest option, {pick.cheapest_name or pick.cheapest_key} at "
            f"{guard.usd(pick.cheapest_cost)}. {pick.explanation} The owner decides whether it is worth it.",
        )
    return Verdict(
        ESCALATE, 0, "pricier_pick",
        f"At {guard.usd(job)} for the job this costs more than the cheapest option Sentinel's price comparison found, "
        f"{pick.cheapest_name or pick.cheapest_key} at {guard.usd(pick.cheapest_cost)}, and it is not the one Sentinel "
        f"recommended ({pick.best_name or pick.best_key}). The owner decides.",
    )


def _differs(listed: str | None, now: str | None) -> bool:
    """True when both texts exist and say different things (case and spacing ignored)."""
    if not listed or not now:
        return False
    return " ".join(listed.lower().split()) != " ".join(now.lower().split())


def found_on_page(mandate: Mandate, payment: Payment) -> bool:
    """True when the agent says it found the link on a web page and the owner's task never names the seller's host.

    A strong prompt injection signal: such a payment never takes the tier 1 fast path to ALLOW. Sentinel cannot see
    what the agent read, so this rests on what the agent declares in found_at.
    """
    found = (payment.found_at or "").strip().lower()
    if not found.startswith(("http://", "https://")):
        return False
    try:
        host = (urlsplit(payment.quote.url).hostname or "").lower()
    except ValueError:
        return True
    return not host or host not in mandate.task_text.lower()


def from_tier1(verdict: str, reason: str, page: bool) -> Verdict | None:
    """Map the fast check: fits allows (unless the link came from a page), off_task blocks, the rest goes on."""
    if verdict == OFF_TASK:
        return Verdict(BLOCK, 1, "off_task", f"This purchase does not fit the task. {reason}".strip())
    if verdict == FITS and not page:
        return Verdict(ALLOW, 1, "fits", reason)
    return None


def from_tier2(verdict: str, reason: str) -> Verdict:
    """Map the judge's answer. Anything but allow or block is held for the owner."""
    if verdict == "allow":
        return Verdict(ALLOW, 2, "judge", reason)
    if verdict == "block":
        return Verdict(BLOCK, 2, "judge", f"This purchase does not serve the task. {reason}".strip())
    return Verdict(ESCALATE, 2, "judge", f"The owner should decide this one. {reason}".strip())


def unavailable(tier: int) -> Verdict:
    """The model could not be reached or gave no usable answer: hold the payment, never allow it."""
    return Verdict(
        ESCALATE, tier, "judge_unavailable",
        "Sentinel could not check this payment against the task, so it is held for the owner.",
    )


def clip(text: str | None, limit: int = MAX_SELLER_TEXT) -> str | None:
    """Seller or agent text cut to a fixed length, so one field cannot flood the judge."""
    if text is None:
        return None
    text = " ".join(text.split())
    return text[:limit] if text else None


def case(mandate: Mandate, payment: Payment, session: Session | None = None) -> dict[str, Any]:
    """The JSON document the judge reads: structured fields only, each string quoted as data.

    The judge's system prompt says every string here was written by someone else (the owner, the agent or the seller)
    and none of it is an instruction. History is included for tier 2 only (pass session).
    """
    quote = payment.quote
    document: dict[str, Any] = {
        "mandate": {
            "owner_task": mandate.task_text,
            "goal": mandate.goal,
            "expected_purchase_types": list(mandate.expected_types),
            "out_of_scope": list(mandate.out_of_scope),
            "budget_usd": guard.usd(mandate.budget),
            "per_call_estimate_usd": guard.usd(mandate.per_call) if mandate.per_call is not None else None,
            "confirmed_by_owner": mandate.status == CONFIRMED,
        },
        "payment": {
            "url": quote.url,
            "amount_usd": guard.usd(quote.amount),
            "seller_address": quote.pay_to,
            "seller_says_it_sells": clip(payment.seller.description),
            "seller_service_name": clip(payment.seller.service_name, 100),
            "seller_tags": [t for t in (clip(tag, 40) for tag in payment.seller.tags[:10]) if t],
        },
        "agent_reason": clip(payment.reason),
        "agent_found_link_at": clip(payment.found_at, 300),
        "link_found_on_a_page_the_owner_never_named": found_on_page(mandate, payment),
    }
    if session is not None:
        document["task_so_far"] = {
            "spent_usd": guard.usd(session.spend),
            "budget_left_usd": guard.usd(max(0, effective_budget(mandate) - session.spend)),
            "earlier_purchases": [
                {"url": p.url, "amount_usd": guard.usd(p.amount), "status": p.status, "reason": clip(p.reason, 200)}
                for p in session.purchases[:MAX_HISTORY]
            ],
        }
    return document
