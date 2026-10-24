"""The x402 payment flow with the guard in the middle. This is the only code that signs.

pay_for() asks the seller for a price, builds a Quote, lets guard.evaluate decide, and only then
signs with the Sentinel wallet and pays. Every outcome is written to the attempt log, and a settled
payment is posted to the ledger. Payment details (payTo, amount) come from the seller's quote and are
authorized by the approved seller list, never by text the agent read online.
"""
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from cdp.evm_local_account import EvmLocalAccount
from x402 import x402Client
from x402.http import x402HTTPClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmScheme

import attempts
import db
import guard
import ledger

NETWORK = "eip155:84532"  # Base Sepolia testnet
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


@dataclass
class PayResult:
    """Outcome of a request: the HTTP status and body, plus the tx hash and quote if a payment was made."""

    status: int
    body: str
    tx_hash: str | None = None
    quote: guard.Quote | None = None


class PaymentRejected(Exception):
    """The guard blocked the payment, or the seller offered nothing we can pay. Nothing was signed."""


class NeedsApproval(Exception):
    """The guard paused the payment for a human. Nothing was signed; a pending attempt row exists."""


def select_requirement(payment_required: Any) -> Any:
    """Pick the Base Sepolia USDC option from the seller's accepts list, or raise PaymentRejected."""
    for requirement in payment_required.accepts:
        if requirement.network == NETWORK and requirement.asset.lower() == USDC_BASE_SEPOLIA.lower():
            return requirement
    offered = [(r.network, r.asset) for r in payment_required.accepts]
    raise PaymentRejected(f"no Base Sepolia USDC option in accepts: {offered}")


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


def authorize(conn: sqlite3.Connection, agent: str, reason: str | None, quote: guard.Quote) -> int:
    """Run the guard and record the attempt. Returns the approved attempt id, or raises.

    BLOCK raises PaymentRejected and NEEDS_APPROVAL raises NeedsApproval. Both leave a row in the log
    so the ledger shows what did not happen.
    """
    quote_dict = asdict(quote)
    history = guard.History(**attempts.load_history(conn, agent, quote_dict, guard.POLICY))
    decision = guard.evaluate(guard.Agent(**attempts.load_agent(conn, agent)), quote, history)
    rule = f"{decision.rule}: {decision.detail}"

    if decision.decision == guard.BLOCK:
        attempts.record_attempt(conn, agent, quote.url, reason, quote_dict, "blocked", decision.decision, rule)
        raise PaymentRejected(rule)
    if decision.decision == guard.NEEDS_APPROVAL:
        try:
            attempts.record_attempt(conn, agent, quote.url, reason, quote_dict, "pending", decision.decision, rule)
        except sqlite3.IntegrityError:
            pass  # the same request is already waiting for a human, so do not log a second pending row
        raise NeedsApproval(rule)
    try:
        return attempts.record_attempt(conn, agent, quote.url, reason, quote_dict, "approved", decision.decision, rule)
    except sqlite3.IntegrityError:
        # Another call already holds the in flight lock for this exact payment.
        rule = "duplicate: identical payment already in flight"
        attempts.record_attempt(conn, agent, quote.url, reason, quote_dict, "blocked", guard.BLOCK, rule)
        raise PaymentRejected(rule) from None


async def pay_for(url: str, account: Any, agent: str = "research-agent", reason: str | None = None) -> PayResult:
    """Fetch url, paying the seller if it returns 402 and the guard allows it.

    account is the CDP account that signs. Returns the response and, if paid, the tx hash. Raises
    PaymentRejected or NeedsApproval when the guard stops the payment.
    """
    conn = db.connect()
    db.init(conn)
    client = x402Client()
    client.register(NETWORK, ExactEvmScheme(EthAccountSigner(EvmLocalAccount(account))))
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
            attempts.record_attempt(conn, agent, url, reason, status="blocked", decision=guard.BLOCK, rule_fired=str(e))
            raise
        quote = build_quote(url, requirement)
        attempt_id = authorize(conn, agent, reason, quote)

        # Sign only the requirement we vetted, not whatever else the seller listed.
        narrowed = payment_required.model_copy(update={"accepts": [requirement]})
        payload = await client.create_payment_payload(narrowed)
        try:
            paid = await session.get(url, headers=http.encode_payment_signature_header(payload))
        except Exception:
            attempts.set_attempt_status(conn, attempt_id, "failed")
            raise

    try:
        tx_hash: str | None = http.get_payment_settle_response(paid.headers.get).transaction
    except ValueError:  # no PAYMENT-RESPONSE header, so nothing settled
        tx_hash = None
    if paid.status_code == 200 and tx_hash:
        ledger.settle_attempt(conn, attempt_id, tx_hash, paid.content)
    else:
        attempts.set_attempt_status(conn, attempt_id, "failed")
    return PayResult(paid.status_code, paid.text, tx_hash, quote)
