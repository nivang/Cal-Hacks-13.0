"""The x402 payment flow with the guard in the middle. This is the only code that signs.

pay_for() asks the seller for a price, builds a Quote, lets guard.evaluate decide, and only then
signs with the calling user's wallet and pays. Every outcome is written to the attempt log, and a settled
payment is posted to the ledger. Payment details (payTo, amount) come from the seller's quote and are
authorized by the approved seller list, never by text the agent read online. Which wallet signs is
decided in wallets.py; this file only sees the x402 signer interface.
"""
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from x402 import NoMatchingRequirementsError, x402Client
from x402.client_base import DEFAULT_MAX_AMOUNT_PER_PAYMENT
from x402.http import x402HTTPClient
from x402.mechanisms.evm import ClientEvmSigner
from x402.mechanisms.evm.exact import ExactEvmScheme

from sentinel import attempts, db, guard, ledger

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


def authorize(conn: db.Connection, agent: str, reason: str | None, quote: guard.Quote) -> int:
    """Run the guard and record the attempt. Returns the approved attempt id, or raises PaymentRejected.

    A block leaves a row in the log so the ledger shows what did not happen, and the exception carries its id.
    """
    quote_dict = asdict(quote)
    history = guard.History(**attempts.load_history(conn, agent, quote_dict, guard.POLICY))
    decision = guard.evaluate(guard.Agent(**attempts.load_agent(conn, agent)), quote, history)
    rule = f"{decision.rule}: {decision.detail}"

    if decision.decision == guard.BLOCK:
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", decision.decision, rule
        )
        raise PaymentRejected(decision.rule, decision.detail, blocked_id, decision.related_id)
    try:
        with db.transaction(conn):  # own savepoint, so a lock clash does not poison an outer transaction
            return attempts.record_attempt(
                conn, agent, quote.url, reason, quote_dict, "approved", decision.decision, rule
            )
    except db.UniqueViolation:
        # Another call already holds the in flight lock for this exact payment.
        live_id = attempts.find_live_id(conn, agent, quote_dict)
        detail = f"An identical payment is already in progress (request {live_id})."
        blocked_id = attempts.record_attempt(
            conn, agent, quote.url, reason, quote_dict, "blocked", guard.BLOCK, f"duplicate: {detail}"
        )
        raise PaymentRejected("duplicate", detail, blocked_id, live_id) from None


async def pay_for(url: str, signer: ClientEvmSigner, *, agent: str, reason: str | None = None) -> PayResult:
    """Fetch url, paying the seller if it returns 402 and the guard allows it.

    signer is the wallet that signs: the calling user's own, from wallets.open_user_wallet(). agent is that
    user's agent, whose limits and history the guard applies; there is no default, so every caller names one.
    Returns the response and, if paid, the tx hash. Raises PaymentRejected when the guard stops the payment,
    and PaymentFailed when an approved payment does not complete.
    """
    with db.connection() as conn:
        return await _pay_with(conn, url, signer, agent, reason)


async def _pay_with(
    conn: db.Connection, url: str, signer: ClientEvmSigner, agent: str, reason: str | None
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
        attempt_id = authorize(conn, agent, reason, quote)

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
