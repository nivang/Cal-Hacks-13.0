"""Tests for payments._pay_with end to end, with a fake seller (httpx MockTransport) and a fake x402 signer.

No network and no wallet: only the flow and what it records are real. Covers the happy path, a free resource,
and every way an approved payment can fail, so a failure never leaves an attempt stuck holding the lock.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from conftest import ASSET, DATACO, NETWORK
from x402 import NoMatchingRequirementsError, x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentRequired, PaymentRequirements

from sentinel import attempts, ledger, payments, results

URL = "http://seller.test/benchmarks/email"
REQUIREMENT = SimpleNamespace(network=NETWORK, asset=ASSET, amount="500000", pay_to=DATACO, max_timeout_seconds=300)


class FakeX402Client:
    """Stands in for x402Client. fail_signing makes create_payment_payload raise."""

    fail_signing = False
    payload_error = None

    def register(self, *_args):
        pass

    async def create_payment_payload(self, _payment_required):
        if self.payload_error:
            raise self.payload_error
        if self.fail_signing:
            raise RuntimeError("signing exploded")
        return "payload"


class FakeHTTPClient:
    """Stands in for x402HTTPClient. settle_hash None means the PAYMENT-RESPONSE header is missing."""

    settle_hash: str | None = "0xtx"
    accepts = [REQUIREMENT]

    def __init__(self, _client):
        pass

    def get_payment_required_response(self, _get_header, _body):
        return SimpleNamespace(accepts=self.accepts, model_copy=lambda update: SimpleNamespace(**update))

    def encode_payment_signature_header(self, _payload):
        return {"PAYMENT-SIGNATURE": "sig"}

    def get_payment_settle_response(self, _get_header):
        if self.settle_hash is None:
            raise ValueError("no header")
        return SimpleNamespace(transaction=self.settle_hash)


@pytest.fixture
def seller(monkeypatch):
    """Patch the x402 pieces and the HTTP client; returns a dict to configure the fake seller per test."""
    config = {"paywalled": True, "paid_status": 200, "paid_body": b'{"vendor":"DataCo"}', "paid_requests": []}
    fake_client = FakeX402Client()
    FakeHTTPClient.settle_hash, FakeHTTPClient.accepts = "0xtx", [REQUIREMENT]

    def handler(request: httpx.Request) -> httpx.Response:
        if not config["paywalled"]:
            return httpx.Response(200, content=b"free page")
        if "PAYMENT-SIGNATURE" in request.headers:
            config["paid_requests"].append(request)
            if config.get("cancel_send"):
                raise asyncio.CancelledError  # the call is cancelled while the payment is being sent
            return httpx.Response(config["paid_status"], content=config["paid_body"],
                                  headers={"content-type": "application/json"})
        return httpx.Response(402, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(payments.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(payments, "x402Client", lambda: fake_client)
    monkeypatch.setattr(payments, "x402HTTPClient", FakeHTTPClient)

    def scheme(signer):
        config["signer"] = signer  # what the flow handed to x402
        return signer

    monkeypatch.setattr(payments, "ExactEvmScheme", scheme)
    config["client"] = fake_client
    return config


async def pay(conn, signer=None):
    reason = "need DataCo email benchmarks for the Q3 report"
    return await payments._pay_with(conn, URL, signer or object(), "research-agent", reason)


@pytest.mark.anyio
async def test_the_signer_reaches_x402_unwrapped(conn, seller):
    signer = object()
    await pay(conn, signer)
    assert seller["signer"] is signer  # the flow adds no wallet specific wrapping; wallets.py owns that


@pytest.mark.anyio
async def test_happy_path_settles_books_and_stores_the_body(conn, seller):
    result = await pay(conn)
    assert (result.settled, result.status, result.tx_hash) == (True, 200, "0xtx")
    assert result.quote is not None and result.quote.amount == 500_000
    assert result.attempt_id is not None
    attempt = attempts.get_attempt(conn, result.attempt_id, "research-agent")
    assert attempt is not None and attempt["status"] == "settled"
    assert attempts.load_result(conn, result.attempt_id) == '{"vendor":"DataCo"}'
    assert conn.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"] == 1


@pytest.mark.anyio
async def test_free_resource_is_returned_and_nothing_is_recorded(conn, seller):
    seller["paywalled"] = False
    result = await pay(conn)
    assert (result.status, result.body, result.attempt_id, result.settled) == (200, "free page", None, False)
    assert conn.execute("SELECT COUNT(*) AS n FROM payment_attempts").fetchone()["n"] == 0


@pytest.mark.anyio
async def test_signing_failure_marks_the_attempt_failed_and_releases_the_lock(conn, seller):
    seller["client"].fail_signing = True
    with pytest.raises(payments.PaymentFailed) as e:
        await pay(conn)
    attempt = attempts.get_attempt(conn, e.value.attempt_id, "research-agent")
    assert attempt is not None and attempt["status"] == "failed"
    seller["client"].fail_signing = False
    assert (await pay(conn)).settled  # the same payment can be retried, the lock is free


@pytest.mark.anyio
async def test_a_cancelled_send_marks_the_attempt_failed_and_still_cancels(conn, seller):
    seller["cancel_send"] = True
    with pytest.raises(asyncio.CancelledError):
        await pay(conn)
    rows = conn.execute("SELECT status FROM payment_attempts").fetchall()
    assert [row["status"] for row in rows] == ["failed"]
    seller["cancel_send"] = False
    assert (await pay(conn)).settled  # the lock is free again


@pytest.mark.anyio
async def test_seller_error_after_payment_request_marks_failed_without_booking(conn, seller):
    seller["paid_status"] = 500
    FakeHTTPClient.settle_hash = None
    result = await pay(conn)
    assert result.settled is False and result.tx_hash is None
    assert result.attempt_id is not None
    attempt = attempts.get_attempt(conn, result.attempt_id, "research-agent")
    assert attempt is not None and attempt["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"] == 0


@pytest.mark.anyio
async def test_booking_failure_after_payment_leaves_the_attempt_approved_and_reports_the_tx(conn, seller, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "settle_attempt", boom)
    with pytest.raises(payments.PaymentFailed) as e:
        await pay(conn)
    assert e.value.tx_hash == "0xtx"
    attempt = attempts.get_attempt(conn, e.value.attempt_id, "research-agent")
    assert attempt is not None and attempt["status"] == "approved"


@pytest.mark.anyio
async def test_seller_without_base_sepolia_usdc_is_rejected_and_logged(conn, seller):
    FakeHTTPClient.accepts = [SimpleNamespace(network="eip155:1", asset="0xother", amount="1", pay_to=DATACO,
                                              max_timeout_seconds=300)]
    with pytest.raises(payments.PaymentRejected) as e:
        await pay(conn)
    assert e.value.rule == "payment_option" and e.value.attempt_id is not None
    attempt = attempts.get_attempt(conn, e.value.attempt_id, "research-agent")
    assert attempt is not None and attempt["status"] == "blocked"


@pytest.mark.anyio
async def test_guard_block_is_raised_before_anything_is_signed(conn, seller):
    FakeHTTPClient.accepts = [SimpleNamespace(network=NETWORK, asset=ASSET, amount="15000000", pay_to=DATACO,
                                              max_timeout_seconds=300)]
    seller["client"].fail_signing = True  # would blow up if signing were reached
    with pytest.raises(payments.PaymentRejected) as e:
        await pay(conn)
    assert e.value.rule == "cap"


@pytest.mark.anyio
async def test_real_x402_spend_cap_is_blocked_before_signing_and_persisted(conn, seller, monkeypatch):
    """Exercise the actual SDK default cap; neither Coinbase nor the seller receives a payment."""
    from unittest.mock import Mock

    requirement = PaymentRequirements(
        scheme="exact", network=NETWORK, asset=ASSET, amount="9000000", pay_to=DATACO,
        max_timeout_seconds=300,
    )
    required = PaymentRequired(accepts=[requirement])
    monkeypatch.setattr(FakeHTTPClient, "get_payment_required_response", lambda *_: required)
    monkeypatch.setattr(payments, "x402Client", x402Client)
    monkeypatch.setattr(payments, "ExactEvmScheme", ExactEvmScheme)
    signer = Mock()
    signer.address = DATACO

    with pytest.raises(payments.PaymentRejected) as error:
        await pay(conn, signer)

    rejected = error.value
    assert rejected.rule == "x402_cap"
    assert "$1 per-payment cap" in rejected.detail
    signer.sign_typed_data.assert_not_called()
    assert seller["paid_requests"] == []
    row = attempts.get_attempt(conn, rejected.attempt_id, "research-agent")
    assert row is not None and row["status"] == "blocked" and row["decision"] == "BLOCK"
    assert row["amount"] == 9_000_000 and row["tx_hash"] is None
    assert results.from_attempt(row, None) == results.blocked(rejected.attempt_id, rejected.rule, rejected.detail)
    assert conn.execute("SELECT COUNT(*) AS n FROM payment_attempts").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"] == 0

    # Repeat the refusal: a blocked request must not hold a payment lock or count against the budget.
    with pytest.raises(payments.PaymentRejected) as retry:
        await pay(conn, signer)
    assert retry.value.rule == "x402_cap"


@pytest.mark.anyio
@pytest.mark.parametrize("message", [
    "No payment requirements match registered schemes",
    "All requirements filtered out by policies",
    "All payment requirements were rejected by spend_controls.allowed_assets max_amount_per_payment",
])
async def test_other_x402_selection_errors_are_blocked_without_claiming_a_dollar_cap(conn, seller, message):
    seller["client"].payload_error = NoMatchingRequirementsError(message)
    with pytest.raises(payments.PaymentRejected) as error:
        await pay(conn)
    assert error.value.rule == "payment_option" and "$1" not in error.value.detail
    assert seller["paid_requests"] == []
    row = attempts.get_attempt(conn, error.value.attempt_id, "research-agent")
    assert row is not None and row["status"] == "blocked" and row["decision"] == "BLOCK"
    assert results.from_attempt(row, None).rule == "payment_option"
    seller["client"].payload_error = None
    assert (await pay(conn)).settled  # the refused attempt released its lock
