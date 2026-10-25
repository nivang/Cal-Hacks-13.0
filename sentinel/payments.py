"""The x402 payment flow with the guard in the middle. This is the only code that signs.

pay_for() asks the seller for a price, builds a Quote, lets guard.evaluate decide, and only then
signs with the calling user's wallet and pays. Sentinel makes the final call on every payment, ALLOW or BLOCK; nothing
waits for a human. Every outcome is written to the attempt log, and a settled payment is posted to the ledger. Payment
details (payTo, amount) come from the seller's quote, never from text the agent read online. Which wallet signs is
decided in wallets.py; this file only sees the x402 signer interface.

Before the guard runs, the payee's Base Sepolia profile is read (chain.cached_profile); a seller not on the approved
list is paid only through escrow (guard rule seller_escrow). A payment made under a task (Task, from the MCP pay
tool) is also checked against the owner's intent once the guard allows it: review_intent runs intent.py's tiers
(rules, the injection screen, then the judge's fast check,
then the judge) and may block it. Without a task (a direct pay_for call, not the MCP tool) only the guard runs.

When the seller offers x402 auth-capture with Sentinel as operator (escrow.check_terms), the money is held in escrow
instead of paid outright: _pay_escrow holds it, fetches the data, checks the delivery (delivery.py, then the judge),
and captures (seller paid) only when the delivery passes; anything else is voided (buyer refunded). sweep retries
captures and voids that failed onchain. Sellers without escrow are paid with exact as before; their deliveries are
checked by the rules and recorded, but nothing can be given back.
"""
import asyncio
import logging
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

import httpx
from x402 import NoMatchingRequirementsError, x402Client
from x402.client_base import DEFAULT_MAX_AMOUNT_PER_PAYMENT
from x402.http import x402HTTPClient
from x402.mechanisms.evm import ClientEvmSigner
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentPayload

from sentinel import (
    attempts,
    chain,
    db,
    delivery,
    escrow,
    guard,
    injection_detector,
    intent,
    judge,
    ledger,
    live,
    mandates,
)

log = logging.getLogger("sentinel.payments")

NETWORK = "eip155:84532"  # Base Sepolia testnet
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


@dataclass
class PayResult:
    """Outcome of a request: the HTTP status and body, plus the quote and tx hash if a payment was made.

    settled is True only when the seller returned the data and the payment was booked. A free resource
    has no quote and no attempt_id, since nothing was paid or recorded. An escrowed payment says what became of the
    hold (escrow: captured, voided, or processing while Sentinel retries) and the delivery check's verdict; a voided
    one carries no body, since the seller is not paid for it.
    """

    status: int
    body: str
    tx_hash: str | None = None
    quote: guard.Quote | None = None
    attempt_id: int | None = None
    settled: bool = False
    content_type: str | None = None
    escrow: str | None = None
    verdict: delivery.Verdict | None = None


class PaymentRejected(Exception):
    """The guard or intent review blocked the payment, or the seller offered nothing we can pay. Nothing was signed.

    rule is the guard rule that fired, detail is the plain English reason, attempt_id is the blocked row in
    the log, and related_id is the earlier attempt a duplicate block refers to. str() is "rule: detail".
    """

    def __init__(self, rule: str, detail: str, attempt_id: int | None = None, related_id: int | None = None):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail
        self.attempt_id, self.related_id = attempt_id, related_id


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
            "intent_reason": self.verdict.detail, **(self.verdict.ml.columns() if self.verdict.ml else {}),
        }


class PaymentFailed(Exception):
    """The payment was approved but did not complete.

    If tx_hash is set the money moved onchain but could not be booked, and the attempt is left approved on
    purpose so it shows up for reconciliation. Otherwise the attempt is marked failed and the lock is released.
    """

    def __init__(self, message: str, attempt_id: int, tx_hash: str | None = None):
        super().__init__(message)
        self.attempt_id, self.tx_hash = attempt_id, tx_hash


def select_requirement(payment_required: Any, operator: str | None = None, now: int | None = None) -> Any:
    """Pick what to pay with from the seller's accepts list, or raise PaymentRejected.

    An auth-capture offer Sentinel can hold in escrow comes first (only when Sentinel has an operator, and only if
    escrow.check_terms passes it), then the Base Sepolia USDC exact option.
    """
    accepts = payment_required.accepts
    refused = None
    if operator is not None:
        now = int(time.time()) if now is None else now
        for requirement in accepts:
            if getattr(requirement, "scheme", "exact") == escrow.SCHEME:
                refused = escrow.check_terms(requirement, operator, now, guard.POLICY)
                if refused is None:
                    return requirement
    for requirement in accepts:
        if getattr(requirement, "scheme", "exact") == "exact" and requirement.network == NETWORK \
                and requirement.asset.lower() == USDC_BASE_SEPOLIA.lower():
            return requirement
    if refused is not None:
        raise PaymentRejected("escrow_terms", f"The seller's escrow offer is not safe to hold. {refused}")
    offered = [(getattr(r, "scheme", "exact"), r.network, r.asset) for r in accepts]
    raise PaymentRejected("payment_option", f"The seller offers no Base Sepolia USDC payment option: {offered}")


def build_quote(url: str, requirement: Any) -> guard.Quote:
    """Normalize an x402 payment requirement into a Quote (amount as an integer in atomic units)."""
    quote = guard.Quote(
        url=url,
        pay_to=requirement.pay_to,
        amount=int(requirement.amount),
        asset=requirement.asset,
        network=requirement.network,
        timeout_s=requirement.max_timeout_seconds,
        escrow=getattr(requirement, "scheme", "exact") == escrow.SCHEME,
    )
    if quote.amount <= 0 or quote.timeout_s <= 0:
        raise PaymentRejected("quote", "The seller returned an invalid amount or expiry.")
    return quote


def decide(
    conn: db.Connection, agent: str, quote: guard.Quote, profile: chain.WalletProfile | None = None,
) -> guard.Decision:
    """Run the guard on this quote with the ledger facts it needs (and the payee's wallet profile, when known)."""
    return guard.evaluate(guard.Agent(**attempts.load_agent(conn, agent)), quote,
                          guard_history(conn, agent, quote, profile))


def guard_history(conn: db.Connection, agent: str, quote: guard.Quote,
                  profile: chain.WalletProfile | None) -> guard.History:
    """The ledger facts for this quote, plus the payee's scam flag and, for a seller not on the approved list, its
    wallet history in a few words for the guard's reason."""
    facts = attempts.load_history(conn, agent, asdict(quote), guard.POLICY)
    summary = profile.summary() if profile is not None and facts["seller_status"] != "approved" else None
    return guard.History(**facts, payee_flagged=bool(profile and profile.flagged), wallet_summary=summary)


async def payee_profile(conn: db.Connection, quote: guard.Quote) -> chain.WalletProfile | None:
    """What Base Sepolia's explorer says about the payee (cached for an hour), or None when it cannot be read."""
    return await chain.cached_profile(conn, quote.pay_to)


def record(
    conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote, decision: guard.Decision,
    review: Review | None = None,
) -> int:
    """Record the attempt for this decision (and intent review). Returns the approved attempt id, or raises.

    A guard block or an intent block leaves a blocked row and raises PaymentRejected, so the ledger shows what did not
    happen, and the exception carries the row's id.
    """
    quote_dict = asdict(quote)
    facts = with_score(review.columns() if review else None, decision, review)
    rule = f"{decision.rule}: {decision.detail}"

    if decision.decision == guard.BLOCK:
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", decision.decision, rule, facts
        )
        raise PaymentRejected(decision.rule, decision.detail, blocked_id, decision.related_id)
    if review is not None and review.verdict.decision != intent.ALLOW:
        verdict = review.verdict
        attempt_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", guard.BLOCK, f"{verdict.rule}: {verdict.detail}",
            facts,
        )
        raise PaymentRejected(verdict.rule, verdict.detail, attempt_id)
    try:
        with db.transaction(conn):  # own savepoint, so a lock clash does not poison an outer transaction
            return attempts.record_attempt(
                conn, agent, quote.url, reason, quote_dict, "approved", decision.decision, rule, facts
            )
    except db.UniqueViolation:
        # Another call already holds the in flight lock for this exact payment.
        live_id = attempts.find_live_id(conn, agent, quote_dict)
        detail = f"An identical payment is already in progress (request {live_id})."
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", guard.BLOCK, f"duplicate: {detail}", facts
        )
        raise PaymentRejected("duplicate", detail, blocked_id, live_id) from None


def authorize(
    conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote,
    *, review: Review | None = None, profile: chain.WalletProfile | None = None, task: Task | None = None,
    seller: intent.Seller | None = None,
) -> int:
    """Admit one payment under the agent lock, or record a block before raising PaymentRejected.

    A block leaves a row in the log so the ledger shows what did not happen, and the exception carries its id.
    profile is the payee's wallet profile, read before the lock (a network call), for the flag and history checks.
    """
    quote_dict = asdict(quote)
    with db.transaction(conn):
        # Serializes budget checks across processes, including different URLs.
        conn.execute("SELECT id FROM agents WHERE name = %s FOR UPDATE", (agent,)).fetchone()
        decision = guard.evaluate(guard.Agent(**attempts.load_agent(conn, agent)), quote,
                                  guard_history(conn, agent, quote, profile))
        if decision.decision != guard.BLOCK and task is not None:
            review = _recheck_task(conn, agent, quote, reason or "", task, review, profile, seller)
        if quote.amount <= 0 or quote.timeout_s <= 0:
            decision = guard.Decision(guard.BLOCK, "quote", "The seller returned an invalid amount or expiry.")
        rule = f"{decision.rule}: {decision.detail}"
        status = "blocked" if decision.decision == guard.BLOCK else "approved"
        facts = with_score(review.columns() if review else ({"mandate_id": task.mandate_id, "found_at": task.found_at}
                                                            if task else {}), decision, review)
        if review and decision.decision != guard.BLOCK and review.verdict.decision != intent.ALLOW:
            verdict = review.verdict
            status = "blocked"
            decision = guard.Decision(guard.BLOCK, verdict.rule, verdict.detail)
            rule = f"{verdict.rule}: {verdict.detail}"
        attempt_id = attempts.record_attempt(conn, agent, quote.url, reason, quote_dict,
                                             status, decision.decision, rule, facts)
    # Raise only after the transaction commits, so blocks stay in the audit log.
    if decision.decision == guard.BLOCK:
        raise PaymentRejected(decision.rule, decision.detail, attempt_id, decision.related_id)
    return attempt_id


def with_score(facts: dict[str, Any] | None, decision: guard.Decision, review: Review | None) -> dict[str, Any] | None:
    """The attempt's facts with the model score behind the outcome: a guard model's score, unless the intent review
    blocked on a model score of its own (the injection screen), which then wins."""
    if review is not None and review.verdict.ml is not None and review.verdict.decision != intent.ALLOW:
        return facts
    if decision.ml is None:
        return facts
    return {**(facts or {}), **decision.ml.columns()}


def _recheck_task(
    conn: db.Connection, agent: str, quote: guard.Quote, reason: str, task: Task,
    review: Review | None, profile: chain.WalletProfile | None, seller: intent.Seller | None,
) -> Review:
    """Refresh deterministic intent facts under the admission lock, without model or network calls."""
    aid = db.agent_id(conn, agent)
    row = mandates.get(conn, task.mandate_id, aid)
    description = review.seller_description if review else None
    if row is None:
        return Review(intent.Verdict(intent.BLOCK, 0, "no_mandate", "There is no such task for this agent."),
                      task.mandate_id, task.found_at, description)
    payment = intent.Payment(quote, reason, task.found_at, seller or intent.Seller(description=description))
    session = mandates.session_facts(conn, task.mandate_id, aid, quote, guard.POLICY)
    if profile is not None and profile.unused:
        session = replace(session, payee_unused=True)
    verdict = intent.tier0(mandates.to_mandate(row), payment, session,
                           guard.Agent(**attempts.load_agent(conn, agent)), guard.POLICY)
    if verdict is None:
        # Tier 0 never allows, so the earlier review (the injection screen and the model tiers) stands.
        verdict = review.verdict if review is not None else intent.unavailable(1)
    return Review(verdict, task.mandate_id, task.found_at, description)


async def authorize_task(
    conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote, task: Task | None,
    seller: intent.Seller | None = None,
) -> int:
    """Read the payee's wallet profile, run the guard and (under a task) the intent review, then admit under the lock.

    The guard runs again inside authorize; the network and model calls happen here, before the lock is taken.
    """
    profile = await payee_profile(conn, quote)
    decision = decide(conn, agent, quote, profile)
    review = None
    if decision.decision != guard.BLOCK and task is not None:
        review = await review_intent(conn, agent, quote, reason or "", task, seller or intent.Seller(), profile)
    # Never await a model or network while holding a row lock: another synchronous DB call
    # on this event loop could otherwise prevent the lock holder from resuming.
    return authorize(conn, agent, reason, quote, review=review, profile=profile, task=task, seller=seller)


async def review_intent(
    conn: db.Connection, agent: str, quote: guard.Quote, reason: str, task: Task, seller: intent.Seller,
    profile: chain.WalletProfile | None = None,
) -> Review:
    """Run deterministic rules, the optional local injection screen, then Claude (including its cache).

    A configured detector failure or judge failure blocks the payment, never pays.
    """
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

    live.step("Task rules")
    verdict = intent.tier0(mandate, payment, session, guard.Agent(**agent_row), policy)
    # The injection model scores every seller it sees, so its score shows on the payment whatever decided it.
    try:
        score = await asyncio.to_thread(injection_detector.assess, seller, session.listing)
    except Exception as error:
        log.warning("injection screen unavailable (%s)", type(error).__name__)
        score = None
        verdict = verdict or injection_detector.UNAVAILABLE
    if verdict is None:
        verdict = injection_detector.verdict(score)
    if verdict is None:
        verdict = await _model_tiers(conn, task.judge, mandate, payment, session, policy)
    if score is not None and verdict.ml is None:
        verdict = replace(verdict, ml=score)
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
        live.step("Quick check against the task")
        try:
            opinion = await judge_.classify(intent.case(mandate, payment))
        except judge.JudgeError:
            return intent.unavailable(1)
        hit = (opinion.verdict, opinion.reason)
        mandates.remember(conn, mandate.id, quote.pay_to, quote.url, *hit)
    settled = intent.from_tier1(hit[0], hit[1], page)
    if settled is not None:
        return settled
    live.step("The judge decides")
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


OK, FREE = "OK", "FREE"
PREVIEW_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Preview:
    """What pay would decide for one URL right now, found without paying or recording anything.

    verdict is OK (pay would go ahead), BLOCK (pay would refuse it) or FREE (no paywall).
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

    Nothing is paid and nothing reaches the journal. A service Sentinel stops is logged as a blocked preview attempt
    (preview true), once per task and URL, so the owner's payment log
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
    if any(getattr(r, "scheme", "exact") == escrow.SCHEME for r in payment_required.accepts):
        quote = replace(quote, escrow=True)  # pay would hold it in escrow (its terms are checked when paying)
    live.step("Policy guard", pay_to=quote.pay_to, amount=quote.amount)
    profile = await payee_profile(conn, quote)
    decision = decide(conn, agent, quote, profile)
    if decision.decision == guard.BLOCK:
        facts = {"mandate_id": task.mandate_id, "found_at": task.found_at, "seller_description": description,
                 **(decision.ml.columns() if decision.ml else {})}
        _log_preview(conn, agent, url, reason, quote, guard.BLOCK, f"{decision.rule}: {decision.detail}", facts)
        return Preview(url, guard.BLOCK, decision.rule, decision.detail, quote.amount, quote.pay_to, seller)
    review = await review_intent(conn, agent, quote, reason, task, seller, profile)
    verdict = review.verdict
    outcome = OK if verdict.decision == intent.ALLOW else guard.BLOCK
    if outcome != OK:
        _log_preview(conn, agent, url, reason, quote, guard.BLOCK, f"{verdict.rule}: {verdict.detail}",
                     with_score(review.columns(), decision, review) or {})
    return Preview(url, outcome, verdict.rule, verdict.detail, quote.amount, quote.pay_to, seller)


def _log_preview(
    conn: db.Connection, agent: str, url: str, reason: str, quote: guard.Quote | None, decision: str, rule: str,
    facts: dict[str, Any],
) -> None:
    """Write a service a search turned away to the payment log as a blocked preview. Once per task and URL."""
    status = "blocked"
    if attempts.preview_logged(conn, facts["mandate_id"], url, status):
        return
    attempts.record_attempt(
        conn, agent, url, reason, asdict(quote) if quote else None, status, decision, rule, {**facts, "preview": True}
    )


async def pay_for(
    url: str, signer: ClientEvmSigner, *, agent: str, reason: str | None = None, task: Task | None = None,
    operator: escrow.EscrowOperator | None = None, connect: Any = db.connection,
) -> PayResult:
    """Fetch url, paying the seller if it returns 402 and the guard (and, under a task, intent binding) allows it.

    signer is the wallet that signs: the calling user's own, from wallets.open_user_wallet(). agent is that
    user's agent, whose limits and history the guard applies; there is no default, so every caller names one.
    operator is Sentinel's escrow operator (escrow.open_operator); without one, escrow offers are ignored.
    Returns the response and, if paid, the tx hash. Raises PaymentRejected when the guard or intent binding stops
    the payment, and PaymentFailed when an approved payment does not complete.
    """
    with connect() as conn, live.watching(conn, agent, task.mandate_id if task else None, url,
                                          "Asking the seller for its price"):
        return await _pay_with(conn, url, signer, agent, reason, task, operator)


async def _pay_with(
    conn: db.Connection, url: str, signer: ClientEvmSigner, agent: str, reason: str | None, task: Task | None = None,
    operator: escrow.EscrowOperator | None = None,
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
            requirement = select_requirement(payment_required, operator.address if operator else None)
            quote = build_quote(url, requirement)
        except PaymentRejected as e:
            e.attempt_id = attempts.record_attempt(
                conn, agent, url, reason, status="blocked", decision=guard.BLOCK, rule_fired=str(e)
            )
            raise
        live.step("Policy guard", pay_to=quote.pay_to, amount=quote.amount)
        attempt_id = await authorize_task(conn, agent, reason, quote, task, seller_from(payment_required))
        in_escrow = operator is not None and getattr(requirement, "scheme", "exact") == escrow.SCHEME
        live.step("Locking the money in escrow" if in_escrow else "Paying the seller", attempt_id=attempt_id)
        if in_escrow:
            assert operator is not None
            return await _pay_escrow(conn, session, http, operator, signer, agent, payment_required, requirement,
                                     quote, attempt_id, task)

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
        verdict = delivery.check(paid.status_code, content_type, paid.content, delivery.promise_from(payment_required))
        if verdict is not None:  # paid already, so only recorded
            attempts.note_delivery(conn, attempt_id, verdict.outcome, f"{verdict.rule}: {verdict.detail}")
        return PayResult(paid.status_code, paid.text, tx_hash, quote, attempt_id, True, content_type)
    attempts.set_attempt_status(conn, attempt_id, "failed")
    return PayResult(paid.status_code, paid.text, tx_hash, quote, attempt_id, False)


async def _pay_escrow(
    conn: db.Connection, session: httpx.AsyncClient, http: x402HTTPClient, operator: escrow.EscrowOperator,
    signer: ClientEvmSigner, agent: str, payment_required: Any, requirement: Any, quote: guard.Quote,
    attempt_id: int, task: Task | None,
) -> PayResult:
    """Hold the money in escrow, fetch what was paid for, check it, then capture (it passed) or void (anything else).

    If the call is cancelled while the hold is being sent, the money may sit in escrow with no record here; the buyer
    can then reclaim it onchain after the capture deadline (at most escrow_max_hold_s).
    """
    info = escrow.payment_info(requirement, signer.address, int(time.time()), escrow.new_salt())
    payment = escrow.info_hash(info)
    try:
        signature = "0x" + signer.sign_typed_data(*escrow.receive_typed_data(info)).hex()
        # Built before the hold, so nothing that could fail is left between holding the money and asking for the data.
        proof = PaymentPayload(
            payload={
                "authorization": {
                    "from": info.payer, "to": escrow.EIP3009_COLLECTOR, "value": str(info.max_amount),
                    "validAfter": "0", "validBefore": str(info.pre_approval_expiry),
                    "nonce": escrow.signature_nonce(info),
                },
                "signature": signature, "salt": str(info.salt),
            },
            accepted=requirement, resource=getattr(payment_required, "resource", None),
        )
        http.encode_payment_signature_header(proof)  # fails here, not after the hold, if the proof cannot be sent
        authorize_tx = await operator.authorize(info, signature)
    except BaseException as e:
        attempts.set_attempt_status(conn, attempt_id, "failed")  # releases the in flight lock
        if not isinstance(e, Exception):
            raise
        log.warning("escrow hold failed for request %s: %s", attempt_id, e)
        raise PaymentFailed("Sentinel could not hold the payment in escrow", attempt_id) from e
    try:
        ledger.hold_attempt(conn, attempt_id, info.to_json(), payment, info.authorization_expiry, info.refund_expiry,
                            authorize_tx)
    except Exception as e:
        # The money is held onchain but not in the books. Leave the attempt approved so it stands out.
        raise PaymentFailed("the payment is held in escrow but could not be recorded", attempt_id, authorize_tx) from e

    proof.payload["authorizeTx"] = authorize_tx
    live.step("Fetching the data")
    try:
        paid = await session.get(quote.url, headers=http.encode_payment_signature_header(proof))
        status, content_type, body = paid.status_code, paid.headers.get("content-type"), paid.content
    except httpx.HTTPError:
        status, content_type, body = 0, None, b""
    live.step("Checking the delivery")
    verdict = await check_delivery(conn, agent, task, quote.url, delivery.promise_from(payment_required), status,
                                   content_type, body)
    log.info("escrow: request %s delivery %s (%s)", attempt_id, verdict.outcome, verdict.rule)
    ledger.record_delivery(conn, attempt_id, body, content_type, verdict.outcome, verdict.rule, verdict.detail, False)
    live.step("Releasing the money to the seller" if verdict.outcome == delivery.PASS else "Refunding you")
    outcome, tx = await _finish(conn, operator, attempt_id, info, verdict.outcome == delivery.PASS, "sentinel")
    if outcome == "captured":
        text = body.decode("utf-8", errors="replace")
        return PayResult(status, text, tx, quote, attempt_id, True, content_type, outcome, verdict)
    return PayResult(status, "", tx, quote, attempt_id, False, content_type, outcome, verdict)


async def check_delivery(
    conn: db.Connection, agent: str, task: Task | None, url: str, promise: delivery.Promise, status: int,
    content_type: str | None, body: bytes,
) -> delivery.Verdict:
    """The delivery rules, then (under a task) the judge. Anything but a pass (a judge failure too) is refunded."""
    verdict = delivery.check(status, content_type, body, promise)
    if verdict is not None:
        return verdict
    if task is None:
        return delivery.without_task()
    row = mandates.get(conn, task.mandate_id, db.agent_id(conn, agent))
    if row is None:
        return delivery.unavailable()
    try:
        opinion = await task.judge.check_delivery(delivery.case(mandates.to_mandate(row), promise, url, body))
    except judge.JudgeError:
        return delivery.unavailable()
    return delivery.from_judge(opinion.verdict, opinion.reason)


async def _finish(
    conn: db.Connection, operator: escrow.EscrowOperator, attempt_id: int, info: escrow.PaymentInfo, capture: bool,
    decided_by: str,
) -> tuple[str, str | None]:
    """Capture or void a hold onchain and book it. Returns (captured or voided, tx hash), or (processing, None) when
    the chain call failed, for the sweeper to try again. Raises PaymentFailed when the chain moved but the books did
    not take it."""
    try:
        tx = await (operator.capture(info, info.max_amount) if capture else operator.void(info))
    except escrow.EscrowError as e:
        log.warning("escrow %s failed for request %s: %s", "capture" if capture else "void", attempt_id, e)
        return "processing", None
    try:
        (ledger.capture_attempt if capture else ledger.void_attempt)(conn, attempt_id, tx, decided_by)
    except Exception as e:
        raise PaymentFailed("the escrow settled but could not be recorded", attempt_id, tx) from e
    return ("captured" if capture else "voided"), tx




async def sweep(conn: db.Connection, operator: escrow.EscrowOperator, policy: dict[str, int] = guard.POLICY) -> int:
    """Finish what is still in escrow. Returns how many holds it settled.

    A passed delivery whose capture failed is captured again, and any other checked one voided again. One whose
    delivery was never checked (after a crash), or one held for the owner before Sentinel decided everything itself,
    is voided once its capture deadline is escrow_deadline_margin_s away, so the buyer never waits for the reclaim
    window.
    """
    margin = policy["escrow_deadline_margin_s"]
    rows = conn.execute(
        "SELECT e.attempt_id, e.payment_info, e.verdict, e.held,"
        " e.capture_deadline <= now() + make_interval(secs => %s) AS due,"
        " e.created_at <= now() - make_interval(secs => %s) AS stale"
        " FROM escrow_payments e JOIN payment_attempts a ON a.id = e.attempt_id"
        " WHERE e.state = 'authorized' AND a.status = 'escrowed' ORDER BY e.capture_deadline",
        (margin, margin),
    ).fetchall()
    settled = 0
    for row in rows:
        info = escrow.PaymentInfo.from_json(row["payment_info"])
        if row["verdict"] is not None and not row["held"]:
            outcome, _ = await _finish(conn, operator, row["attempt_id"], info, row["verdict"] == delivery.PASS,
                                       "sentinel")
        elif row["due"] or (row["verdict"] is None and row["stale"]):
            outcome, _ = await _finish(conn, operator, row["attempt_id"], info, False, "deadline")
        else:
            continue
        settled += outcome != "processing"
    return settled
