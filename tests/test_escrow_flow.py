"""Tests for the escrow path of payments._pay_with and sweep: a fake seller, signer and operator.

Only the flow and what it records are real. The seller offers auth-capture with the fake operator plus exact, so
these also cover choosing between them.
"""
import time
from types import SimpleNamespace

import httpx
import pytest
from conftest import ASSET, DATACO, NETWORK, FakeJudge
from x402.schemas import PaymentRequirements, ResourceInfo

from sentinel import attempts, db, escrow, ledger, mandates, payments

URL = "http://seller.test/benchmarks/email"
BUYER = "0x" + "be" * 20
EXACT = SimpleNamespace(network=NETWORK, asset=ASSET, amount="200000", pay_to=DATACO, max_timeout_seconds=300)
GOOD = b'{"vendor": "DataCo", "open_rate": 0.214}'

pytestmark = pytest.mark.anyio


class FakeOperator:
    """Records every escrow call. fail names a call (authorize, capture, void) that raises EscrowError."""

    def __init__(self) -> None:
        self.address = "0x" + "0e" * 20
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def _call(self, name: str) -> str:
        self.calls.append(name)
        if name in self.fail:
            raise escrow.EscrowError(f"{name} reverted")
        return f"0x{name}{len(self.calls)}"

    async def authorize(self, info, buyer_signature):
        return await self._call("authorize")

    async def capture(self, info, amount):
        return await self._call("capture")

    async def void(self, info):
        return await self._call("void")


class FakeSigner:
    address = BUYER

    def sign_typed_data(self, domain, types, primary_type, message):
        assert primary_type == "ReceiveWithAuthorization" and message["from"].lower() == BUYER
        return b"\x01" * 65


def offer(operator: str, **extra) -> PaymentRequirements:
    now = int(time.time())
    terms = {"name": "USDC", "version": "2", "authCaptureEscrow": escrow.ESCROW, "captureAuthorizer": operator,
             "operatorType": "delegated", "paymentFlow": "escrow", "captureDeadline": now + 1800,
             "refundDeadline": now + 3600, "minFeeBps": 0, "maxFeeBps": 0, "feeRecipient": escrow.ZERO,
             "assetTransferMethod": "eip3009", **extra}
    return PaymentRequirements(scheme="auth-capture", network=NETWORK, asset=ASSET, amount="200000", pay_to=DATACO,
                               max_timeout_seconds=300, extra=terms)


@pytest.fixture
def operator():
    return FakeOperator()


@pytest.fixture
def seller(monkeypatch, operator):
    """A seller offering escrow (with this operator) and exact. Configure what a paid call returns per test."""
    config = {"status": 200, "body": GOOD, "type": "application/json",
              "accepts": [offer(operator.address), EXACT], "headers": []}

    class FakeHTTPClient:
        def __init__(self, _client):
            pass

        def get_payment_required_response(self, _get_header, _body):
            resource = ResourceInfo(url=URL, mime_type="application/json", description="Email benchmarks.",
                                    service_name="DataCo")
            return SimpleNamespace(accepts=config["accepts"], resource=resource, extensions=None,
                                   model_copy=lambda update: SimpleNamespace(**update))

        def encode_payment_signature_header(self, payload):
            config["proof"] = payload
            return {"PAYMENT-SIGNATURE": "sig"}

        def get_payment_settle_response(self, _get_header):
            return SimpleNamespace(transaction="0xexact")

    class FakeX402Client:
        def register(self, *_args):
            pass

        async def create_payment_payload(self, _payment_required):
            return "payload"

    def handler(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" in request.headers:
            config["headers"].append(request.headers["PAYMENT-SIGNATURE"])
            if config.get("down"):
                raise httpx.ConnectError("seller went away")
            return httpx.Response(config["status"], content=config["body"], headers={"content-type": config["type"]})
        return httpx.Response(402, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(payments.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(payments, "x402Client", FakeX402Client)
    monkeypatch.setattr(payments, "x402HTTPClient", FakeHTTPClient)
    monkeypatch.setattr(payments, "ExactEvmScheme", lambda signer: signer)
    return config


async def pay(conn, operator, task=None):
    return await payments._pay_with(conn, URL, FakeSigner(), "research-agent", "benchmarks for the Q3 report", task,
                                    operator)


def balances(conn) -> dict[str, int]:
    return {r["code"]: r["debit"] - r["credit"] for r in ledger.trial_balance(conn)}


def a_task(conn, judge: FakeJudge) -> payments.Task:
    aid = db.agent_id(conn, "research-agent")
    mandate = mandates.create(conn, aid, "Write the Q3 email report.", "Write the Q3 email report.",
                              ["email benchmarks"], [], 3_000_000, None)
    return payments.Task(mandate, None, judge)


async def test_a_good_delivery_is_held_then_captured(conn, seller, operator):
    result = await pay(conn, operator)
    assert operator.calls == ["authorize", "capture"]
    assert (result.escrow, result.settled, result.tx_hash, result.body) == ("captured", True, "0xcapture2",
                                                                             GOOD.decode())
    assert attempts.get_attempt(conn, result.attempt_id, "research-agent")["status"] == "settled"
    books = balances(conn)
    assert books["1050"] == 0 and books["6100"] == 200_000 and books["1010"] == -200_000
    proof = seller["proof"]
    assert proof.accepted.scheme == "auth-capture" and proof.payload["authorizeTx"] == "0xauthorize1"
    assert proof.payload["authorization"]["to"] == escrow.EIP3009_COLLECTOR


async def test_a_bad_delivery_is_voided_and_withheld(conn, seller, operator):
    seller["body"], seller["type"] = b"<html>Service unavailable</html>", "text/html"
    result = await pay(conn, operator)
    assert operator.calls == ["authorize", "void"]
    assert (result.escrow, result.settled, result.body, result.verdict.rule) == ("voided", False, "", "wrong_type")
    assert attempts.get_attempt(conn, result.attempt_id, "research-agent")["status"] == "voided"
    assert balances(conn)["1010"] == 0
    assert attempts.week_spend(conn, db.agent_id(conn, "research-agent"), 7) == 0


async def test_a_seller_that_never_answers_is_voided(conn, seller, operator):
    seller["down"] = True
    result = await pay(conn, operator)
    assert (result.escrow, result.verdict.rule) == ("voided", "http_error")


async def test_a_doubtful_delivery_is_refunded_without_asking_anyone(conn, seller, operator):
    judge = FakeJudge()
    judge.delivered = "not_delivered"
    result = await pay(conn, operator, a_task(conn, judge))
    assert operator.calls == ["authorize", "void"] and len(judge.deliveries) == 1
    assert (result.escrow, result.settled, result.body, result.verdict.rule) == ("voided", False, "", "not_delivered")
    row = conn.execute("SELECT held, verdict, state, decided_by FROM escrow_payments WHERE attempt_id = %s",
                       (result.attempt_id,)).fetchone()
    assert (row["held"], row["verdict"], row["state"], row["decided_by"]) == (False, "unsure", "voided", "sentinel")
    assert attempts.get_attempt(conn, result.attempt_id, "research-agent")["status"] == "voided"
    assert balances(conn)["1010"] == 0


async def test_a_judge_failure_refunds_never_pays(conn, seller, operator):
    judge = FakeJudge()
    judge.fail_delivery = True
    result = await pay(conn, operator, a_task(conn, judge))
    assert (result.escrow, result.verdict.rule) == ("voided", "judge_unavailable")
    assert operator.calls == ["authorize", "void"]


async def test_a_failed_hold_fails_the_attempt_and_releases_the_lock(conn, seller, operator):
    operator.fail = {"authorize"}
    with pytest.raises(payments.PaymentFailed) as e:
        await pay(conn, operator)
    assert attempts.get_attempt(conn, e.value.attempt_id, "research-agent")["status"] == "failed"
    assert seller["headers"] == []  # the seller was never told it was paid
    operator.fail = set()
    assert (await pay(conn, operator)).escrow == "captured"


async def test_a_failed_capture_is_retried_by_the_sweeper(conn, seller, operator):
    operator.fail = {"capture"}
    result = await pay(conn, operator)
    assert (result.escrow, result.settled) == ("processing", False)
    operator.fail = set()
    assert await payments.sweep(conn, operator) == 1
    assert attempts.get_attempt(conn, result.attempt_id, "research-agent")["status"] == "settled"


async def test_a_failed_refund_is_retried_by_the_sweeper(conn, seller, operator):
    judge = FakeJudge()
    judge.delivered = "unsure"
    operator.fail = {"void"}
    result = await pay(conn, operator, a_task(conn, judge))
    assert (result.escrow, result.settled) == ("processing", False)
    operator.fail = set()
    assert await payments.sweep(conn, operator) == 1
    assert attempts.get_attempt(conn, result.attempt_id, "research-agent")["status"] == "voided"


async def test_the_sweeper_voids_a_payment_held_before_sentinel_decided_alone_near_its_deadline(conn, seller,
                                                                                                 operator):
    operator.fail = {"capture"}
    result = await pay(conn, operator)
    operator.fail = set()
    conn.execute("UPDATE escrow_payments SET held = true, verdict = 'unsure' WHERE attempt_id = %s",
                 (result.attempt_id,))  # a row held for the owner by an older Sentinel
    assert await payments.sweep(conn, operator) == 0  # not due yet
    conn.execute("UPDATE escrow_payments SET capture_deadline = now() + interval '1 minute' WHERE attempt_id = %s",
                 (result.attempt_id,))
    assert await payments.sweep(conn, operator) == 1
    row = conn.execute("SELECT state, decided_by FROM escrow_payments WHERE attempt_id = %s",
                       (result.attempt_id,)).fetchone()
    assert (row["state"], row["decided_by"]) == ("voided", "deadline")


async def test_without_an_operator_the_exact_option_is_paid(conn, seller, operator):
    result = await pay(conn, None)
    assert (result.escrow, result.tx_hash, result.settled) == (None, "0xexact", True)
    assert operator.calls == []


async def test_an_exact_delivery_that_fails_the_rules_is_recorded(conn, seller, operator):
    seller["body"], seller["type"] = b"<html>oops</html>", "text/html"
    result = await pay(conn, None)
    row = conn.execute("SELECT delivery_verdict, delivery_reason FROM payment_attempts WHERE id = %s",
                       (result.attempt_id,)).fetchone()
    assert result.settled and row["delivery_verdict"] == "fail" and row["delivery_reason"].startswith("wrong_type")


async def test_an_unsafe_escrow_offer_falls_back_to_exact(conn, seller, operator):
    seller["accepts"] = [offer("0x" + "99" * 20), EXACT]
    result = await pay(conn, operator)
    assert result.escrow is None and operator.calls == []


async def test_only_an_unsafe_escrow_offer_is_blocked(conn, seller, operator):
    seller["accepts"] = [offer(operator.address, maxFeeBps=500)]
    with pytest.raises(payments.PaymentRejected) as e:
        await pay(conn, operator)
    assert e.value.rule == "escrow_terms" and "fee" in e.value.detail


async def test_failed_deliveries_count_against_the_seller(conn, seller, operator):
    seller["body"], seller["type"] = b"<html>oops</html>", "text/html"
    for _ in range(2):
        assert (await pay(conn, operator)).escrow == "voided"
    facts = mandates.session_facts(conn, a_task(conn, FakeJudge()).mandate_id, db.agent_id(conn, "research-agent"),
                                   payments.build_quote(URL, EXACT))
    assert facts.seller_bad_deliveries == 2
