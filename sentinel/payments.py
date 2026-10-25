"""The x402 payment flow with the guard in the middle. This is the only code that signs.

pay_for() asks the seller for a price, builds a Quote, lets guard.evaluate decide, and only then
signs with the calling user's wallet and pays. Every outcome is written to the attempt log, and a settled
payment is posted to the ledger. Payment details (payTo, amount) come from the seller's quote and are
authorized by the approved seller list, never by text the agent read online. Which wallet signs is
decided in wallets.py; this file only sees the x402 signer interface.

A payment made under a task (Task, from the MCP pay tool) is also checked against the owner's intent once the guard
allows it: review_intent runs intent.py's tiers (rules, then the judge's fast check, then the judge) and may block
the payment or hold it for the owner (PaymentHeld). Without a task (the command line demo) only the guard runs.
"""
import logging
from dataclasses import asdict, dataclass, replace
from typing import Any

import httpx
from x402 import NoMatchingRequirementsError, x402Client
from x402.client_base import DEFAULT_MAX_AMOUNT_PER_PAYMENT
from x402.http import x402HTTPClient
from x402.mechanisms.evm import ClientEvmSigner
from x402.mechanisms.evm.exact import ExactEvmScheme

from sentinel import attempts, chain, db, guard, intent, judge, ledger, mandates

log = logging.getLogger("sentinel.payments")

NETWORK = "eip155:84532"  # Base Sepolia testnet
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


@dataclass
class PayResult:
    """Outcome of a request: the HTTP status and body, plus the quote and tx hash if a payment was made.

    settled is True only when the seller returned the data and the payment was booked. A free resource
    has no quote and no attempt_id, since nothing was paid or recorded.
    """

    status: int
    body: str
    tx_hash: str | None = None
    quote: guard.Quote | None = None
    attempt_id: int | None = None
    settled: bool = False
    content_type: str | None = None


class PaymentRejected(Exception):
    """The guard blocked the payment, or the seller offered nothing we can pay. Nothing was signed.

    rule is the guard rule that fired, detail is the plain English reason, attempt_id is the blocked row in
    the log, and related_id is the earlier attempt a duplicate block refers to. str() is "rule: detail".
    """

    def __init__(self, rule: str, detail: str, attempt_id: int | None = None, related_id: int | None = None):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail
        self.attempt_id, self.related_id = attempt_id, related_id


class PaymentHeld(PaymentRejected):
    """Intent binding held the payment for the owner (ESCALATE). Nothing was signed; the owner may Allow once."""


@dataclass(frozen=True)
class Task:
    """The task a payment is made under: which mandate, where the agent says it found the link, who judges."""

    mandate_id: int
    found_at: str | None
    judge: judge.Judge


@dataclass(frozen=True)
class Review:
    """The outcome of review_intent, recorded with the attempt."""

    verdict: intent.Verdict
    mandate_id: int
    found_at: str | None
    seller_description: str | None

    def columns(self) -> dict[str, Any]:
        """The attempts.INTENT_COLUMNS values for this review."""
        return {
            "mandate_id": self.mandate_id, "found_at": self.found_at, "seller_description": self.seller_description,
            "intent_tier": self.verdict.tier, "intent_verdict": self.verdict.decision,
            "intent_reason": self.verdict.detail,
        }


class PaymentFailed(Exception):
    """The payment was approved but did not complete.

    If tx_hash is set the money moved onchain but could not be booked, and the attempt is left approved on
    purpose so it shows up for reconciliation. Otherwise the attempt is marked failed and the lock is released.
    """

    def __init__(self, message: str, attempt_id: int, tx_hash: str | None = None):
        super().__init__(message)
        self.attempt_id, self.tx_hash = attempt_id, tx_hash


def select_requirement(payment_required: Any) -> Any:
    """Pick the Base Sepolia USDC option from the seller's accepts list, or raise PaymentRejected."""
    for requirement in payment_required.accepts:
        if requirement.network == NETWORK and requirement.asset.lower() == USDC_BASE_SEPOLIA.lower():
            return requirement
    offered = [(r.network, r.asset) for r in payment_required.accepts]
    raise PaymentRejected("payment_option", f"The seller offers no Base Sepolia USDC payment option: {offered}")


def build_quote(url: str, requirement: Any) -> guard.Quote:
    """Normalize an x402 payment requirement into a Quote (amount as an integer in atomic units)."""
    return guard.Quote(
        url=url,
        pay_to=requirement.pay_to,
        amount=int(requirement.amount),
        asset=requirement.asset,
        network=requirement.network,
        timeout_s=requirement.max_timeout_seconds,
    )


def decide(
    conn: db.Connection, agent: str, quote: guard.Quote, profile: chain.WalletProfile | None = None,
) -> guard.Decision:
    """Run the guard on this quote with the ledger facts it needs (and the payee's wallet profile, when known)."""
    history = guard.History(**attempts.load_history(conn, agent, asdict(quote), guard.POLICY),
                            payee_flagged=bool(profile and profile.flagged))
    return guard.evaluate(guard.Agent(**attempts.load_agent(conn, agent)), quote, history)


async def payee_profile(conn: db.Connection, quote: guard.Quote) -> chain.WalletProfile | None:
    """What Base Sepolia's explorer says about the payee (cached for an hour), or None when it cannot be read."""
    return await chain.cached_profile(conn, quote.pay_to)


def record(
    conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote, decision: guard.Decision,
    review: Review | None = None,
) -> int:
    """Record the attempt for this decision (and intent review). Returns the approved attempt id, or raises.

    A guard block or an intent block leaves a blocked row and raises PaymentRejected; an intent hold leaves an
    escalated row and raises PaymentHeld. Either way the ledger shows what did not happen, and the exception carries
    the row's id. An approval that used the owner's Allow once spends it.
    """
    quote_dict = asdict(quote)
    facts = review.columns() if review else None
    rule = f"{decision.rule}: {decision.detail}"

    if decision.decision == guard.BLOCK:
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", decision.decision, rule, facts
        )
        raise PaymentRejected(decision.rule, decision.detail, blocked_id, decision.related_id)
    if review is not None and review.verdict.decision != intent.ALLOW:
        verdict = review.verdict
        held = verdict.decision == intent.ESCALATE
        attempt_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "escalated" if held else "blocked", verdict.decision,
            f"{verdict.rule}: {verdict.detail}", facts,
        )
        raise (PaymentHeld if held else PaymentRejected)(verdict.rule, verdict.detail, attempt_id)
    try:
        with db.transaction(conn):  # own savepoint, so a lock clash does not poison an outer transaction
            approved = attempts.record_attempt(
                conn, agent, quote.url, reason, quote_dict, "approved", decision.decision, rule, facts
            )
            allowance = review.verdict.allowance_id if review else None
            if allowance is not None and not mandates.use_allowance(conn, allowance, approved):
                raise db.UniqueViolation  # another call used this Allow once first
            return approved
    except db.UniqueViolation:
        # Another call already holds the in flight lock for this exact payment.
        live_id = attempts.find_live_id(conn, agent, quote_dict)
        detail = f"An identical payment is already in progress (request {live_id})."
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", guard.BLOCK, f"duplicate: {detail}", facts
        )
        raise PaymentRejected("duplicate", detail, blocked_id, live_id) from None


def authorize(conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote) -> int:
    """Run the guard and record the attempt. Returns the approved attempt id, or raises PaymentRejected."""
    return record(conn, agent, reason, quote, decide(conn, agent, quote))


async def authorize_task(
    conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote, task: Task | None,
    seller: intent.Seller | None = None,
) -> int:
    """The guard first; when it allows and there is a task, the intent review; then record. Returns the attempt id."""
    profile = await payee_profile(conn, quote) if task is not None else None
    decision = decide(conn, agent, quote, profile)
    review = None
    if decision.decision == guard.ALLOW and task is not None:
        review = await review_intent(conn, agent, quote, reason or "", task, seller or intent.Seller(), profile)
    return record(conn, agent, reason, quote, decision, review)


async def review_intent(
    conn: db.Connection, agent: str, quote: guard.Quote, reason: str, task: Task, seller: intent.Seller,
    profile: chain.WalletProfile | None = None,
) -> Review:
    """Check one guard approved payment against its task: tier 0 rules, then the cached or fresh fast check, then
    the judge. A judge failure holds the payment (intent.unavailable), never pays."""
    policy = guard.POLICY
    agent_row = attempts.load_agent(conn, agent)
    aid = db.agent_id(conn, agent)
    row = mandates.get(conn, task.mandate_id, aid)
    description = intent.clip(seller.description)
    if row is None:  # the MCP tool checks ownership first; this only guards other callers
        missing = intent.Verdict(intent.BLOCK, 0, "no_mandate", "There is no such task for this agent.")
        return Review(missing, task.mandate_id, task.found_at, description)
    mandate = mandates.to_mandate(row)
    payment = intent.Payment(quote, reason, task.found_at, seller)
    session = mandates.session_facts(conn, mandate.id, aid, quote, policy)
    if profile is not None and profile.unused:
        session = replace(session, payee_unused=True)

    verdict = intent.tier0(mandate, payment, session, guard.Agent(**agent_row), policy)
    if verdict is None:
        verdict = await _model_tiers(conn, task.judge, mandate, payment, session, policy)
    log.info("intent: agent %s task %s tier %s %s (%s)", agent, mandate.id, verdict.tier, verdict.decision,
             verdict.rule)
    return Review(verdict, mandate.id, task.found_at, description)


async def _model_tiers(
    conn: db.Connection, judge_: judge.Judge, mandate: intent.Mandate, payment: intent.Payment,
    session: intent.Session, policy: dict[str, int],
) -> intent.Verdict:
    """Tier 1 (cached per task, seller and URL), then tier 2 when tier 1 does not settle it."""
    quote = payment.quote
    page = intent.found_on_page(mandate, payment)
    hit = mandates.cached(conn, mandate.id, quote.pay_to, quote.url, policy["intent_cache_ttl_s"])
    if hit is None:
        try:
            opinion = await judge_.classify(intent.case(mandate, payment))
        except judge.JudgeError:
            return intent.unavailable(1)
        hit = (opinion.verdict, opinion.reason)
        mandates.remember(conn, mandate.id, quote.pay_to, quote.url, *hit)
    settled = intent.from_tier1(hit[0], hit[1], page)
    if settled is not None:
        return settled
    try:
        answer = await judge_.decide(intent.case(mandate, payment, session))
    except judge.JudgeError:
        return intent.unavailable(2)
    return intent.from_tier2(answer.verdict, answer.reason)


def seller_from(payment_required: Any) -> intent.Seller:
    """What the seller says it sells, from the 402's resource info (x402 v2). Empty when it says nothing."""
    resource = getattr(payment_required, "resource", None)
    if resource is None:
        return intent.Seller()
    tags = getattr(resource, "tags", None) or ()
    return intent.Seller(
        description=intent.clip(getattr(resource, "description", None)),
        service_name=intent.clip(getattr(resource, "service_name", None), 100),
        tags=tuple(str(t) for t in tags if isinstance(t, str))[:10],
    )


OK, HELD_FOR_OWNER, FREE = "OK", "NEEDS_OWNER", "FREE"
PREVIEW_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Preview:
    """What pay would decide for one URL right now, found without paying or recording anything.

    verdict is OK (pay would go ahead), BLOCKED, NEEDS_OWNER (pay would hold it for the owner) or FREE (no paywall).
    rule and detail say why; amount and pay_to come from the seller's 402. Advice only: pay checks everything again.
    """

    url: str
    verdict: str
    rule: str
    detail: str
    amount: int | None = None
    pay_to: str | None = None
    seller: intent.Seller = intent.Seller()


async def preview(conn: db.Connection, agent: str, url: str, reason: str, task: Task) -> Preview:
    """Ask the seller for its price, then run the guard and the intent review exactly as pay would, without paying.

    Nothing is paid and nothing reaches the journal. A service Sentinel stops (BLOCKED or NEEDS_OWNER) is logged as a
    preview attempt (blocked or escalated, preview true), once per task, URL and status, so the owner's payment log
    shows everything that was turned away while the agent only ever sees what it may buy. The tier 1 verdict is
    cached, so a pay that follows is quick. Network problems raise httpx errors for the caller to report.
    """
    async with httpx.AsyncClient(timeout=PREVIEW_TIMEOUT_S) as session:
        first = await session.get(url)
    if first.status_code != 402:
        return Preview(url, FREE, "free", "This URL is not paywalled, so it costs nothing.")
    payment_required = x402HTTPClient(x402Client()).get_payment_required_response(
        first.headers.get, first.json() if first.content else None
    )
    seller = seller_from(payment_required)
    description = intent.clip(seller.description)
    try:
        requirement = select_requirement(payment_required)
    except PaymentRejected as e:
        _log_preview(conn, agent, url, reason, None, guard.BLOCK, f"{e.rule}: {e.detail}",
                     {"mandate_id": task.mandate_id, "found_at": task.found_at, "seller_description": description})
        return Preview(url, guard.BLOCK, e.rule, e.detail, seller=seller)
    quote = build_quote(url, requirement)
    profile = await payee_profile(conn, quote)
    decision = decide(conn, agent, quote, profile)
    if decision.decision == guard.BLOCK:
        _log_preview(conn, agent, url, reason, quote, guard.BLOCK, f"{decision.rule}: {decision.detail}",
                     {"mandate_id": task.mandate_id, "found_at": task.found_at, "seller_description": description})
        return Preview(url, guard.BLOCK, decision.rule, decision.detail, quote.amount, quote.pay_to, seller)
    review = await review_intent(conn, agent, quote, reason, task, seller, profile)
    verdict = review.verdict
    outcome = {intent.ALLOW: OK, intent.BLOCK: guard.BLOCK}.get(verdict.decision, HELD_FOR_OWNER)
    if outcome != OK:
        _log_preview(conn, agent, url, reason, quote, verdict.decision, f"{verdict.rule}: {verdict.detail}",
                     review.columns())
    return Preview(url, outcome, verdict.rule, verdict.detail, quote.amount, quote.pay_to, seller)


def _log_preview(
    conn: db.Connection, agent: str, url: str, reason: str, quote: guard.Quote | None, decision: str, rule: str,
    facts: dict[str, Any],
) -> None:
    """Write a service a search turned away to the payment log: escalated for a hold, else blocked. Once per task,
    URL and status. A held one can be allowed once from the Tasks page."""
    status = "escalated" if decision == intent.ESCALATE else "blocked"
    if attempts.preview_logged(conn, facts["mandate_id"], url, status):
        return
    attempts.record_attempt(
        conn, agent, url, reason, asdict(quote) if quote else None, status, decision, rule, {**facts, "preview": True}
    )


async def pay_for(
    url: str, signer: ClientEvmSigner, *, agent: str, reason: str | None = None, task: Task | None = None,
) -> PayResult:
    """Fetch url, paying the seller if it returns 402 and the guard (and, under a task, intent binding) allows it.

    signer is the wallet that signs: the calling user's own, from wallets.open_user_wallet(). agent is that
    user's agent, whose limits and history the guard applies; there is no default, so every caller names one.
    Returns the response and, if paid, the tx hash. Raises PaymentRejected when the guard or intent binding stops
    the payment, PaymentHeld when it is held for the owner, and PaymentFailed when an approved payment does not
    complete.
    """
    with db.connection() as conn:
        return await _pay_with(conn, url, signer, agent, reason, task)


async def _pay_with(
    conn: db.Connection, url: str, signer: ClientEvmSigner, agent: str, reason: str | None, task: Task | None = None,
) -> PayResult:
    """The payment flow itself, on a borrowed connection. Split out so pay_for can manage the pool."""
    client = x402Client()
    client.register(NETWORK, ExactEvmScheme(signer))
    http = x402HTTPClient(client)

    async with httpx.AsyncClient() as session:
        first = await session.get(url)
        if first.status_code != 402:
            return PayResult(first.status_code, first.text)  # free resource

        payment_required = http.get_payment_required_response(
            first.headers.get, first.json() if first.content else None
        )
        try:
            requirement = select_requirement(payment_required)
        except PaymentRejected as e:
            e.attempt_id = attempts.record_attempt(
                conn, agent, url, reason, status="blocked", decision=guard.BLOCK, rule_fired=str(e)
            )
            raise
        quote = build_quote(url, requirement)
        attempt_id = await authorize_task(conn, agent, reason, quote, task, seller_from(payment_required))

        try:
            # Sign only the requirement we vetted, not whatever else the seller listed.
            narrowed = payment_required.model_copy(update={"accepts": [requirement]})
            try:
                payload = await client.create_payment_payload(narrowed)
            except NoMatchingRequirementsError as e:
                # Selection and spend controls run before signing. Keep the SDK's advice about disabling
                # safeguards out of the tool response, and persist the same block payment_status will show.
                rule = "payment_option"
                detail = "The x402 client could not accept the seller's payment option. Nothing was signed or sent."
                if "spend_controls.max_amount_per_payment" in str(e):
                    rule = "x402_cap"
                    detail = (
                        f"The seller's price exceeds the x402 client's {DEFAULT_MAX_AMOUNT_PER_PAYMENT} "
                        "per-payment cap. Nothing was signed or sent."
                    )
                attempts.block_attempt(conn, attempt_id, rule, detail)
                raise PaymentRejected(rule, detail, attempt_id) from e
            paid = await session.get(url, headers=http.encode_payment_signature_header(payload))
        except PaymentRejected:
            raise
        except BaseException as e:
            # Also on cancellation (a BaseException), so a dropped call never leaves the attempt approved.
            attempts.set_attempt_status(conn, attempt_id, "failed")  # releases the in flight lock
            if not isinstance(e, Exception):
                raise
            raise PaymentFailed("signing or sending the payment failed", attempt_id) from e

    try:
        tx_hash: str | None = http.get_payment_settle_response(paid.headers.get).transaction
    except ValueError:  # no PAYMENT-RESPONSE header, so nothing settled
        tx_hash = None
    if paid.status_code == 200 and tx_hash:
        content_type = paid.headers.get("content-type")
        try:
            ledger.settle_attempt(conn, attempt_id, tx_hash, paid.content, content_type)
        except Exception as e:
            # Money moved but the books did not take it. Leave the attempt approved so it stands out.
            raise PaymentFailed("payment went through but could not be recorded", attempt_id, tx_hash) from e
        return PayResult(paid.status_code, paid.text, tx_hash, quote, attempt_id, True, content_type)
    attempts.set_attempt_status(conn, attempt_id, "failed")
    return PayResult(paid.status_code, paid.text, tx_hash, quote, attempt_id, False)
