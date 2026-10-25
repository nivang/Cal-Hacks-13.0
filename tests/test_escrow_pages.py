"""Tests for payments in escrow on the account pages: shown read only, since Sentinel captures or refunds them itself.

Overrides the app fixture so it has an escrow operator (a fake that records calls); everything else is conftest's.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from conftest import ASSET, DATACO, NETWORK
from fastapi import FastAPI
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from test_account_pages import BOB_IP, csrf, signed_in, user_of
from test_escrow_flow import FakeOperator

from sentinel import ledger, payments
from sentinel.guard import Quote

pytestmark = pytest.mark.anyio
INFO = {"operator": "0x" + "0e" * 20, "payer": "0x" + "be" * 20, "receiver": DATACO, "token": ASSET,
        "max_amount": 300_000, "pre_approval_expiry": 2_000_000_000, "authorization_expiry": 2_000_001_800,
        "refund_expiry": 2_000_003_600, "min_fee_bps": 0, "max_fee_bps": 0,
        "fee_receiver": "0x0000000000000000000000000000000000000000", "salt": "5"}


@pytest.fixture
def operator() -> FakeOperator:
    return FakeOperator()


@pytest.fixture
async def app(settings, connect, mailer, fake_wallets, pay_spy, fake_judge, operator) -> AsyncIterator[FastAPI]:
    """conftest's app, plus an escrow operator."""
    from sentinel.web.app import create_app

    @asynccontextmanager
    async def open_operator() -> AsyncIterator[FakeOperator]:
        yield operator

    application = create_app(settings, connect=connect, mailer=mailer, pay_fn=pay_spy, judge_=fake_judge,
                             open_operator=open_operator)

    async def with_lifespan_state(scope: Any, receive: Any, send: Any) -> None:
        await application({**scope, "state": {}}, receive, send)

    async with run_asgi_lifespan(with_lifespan_state):
        yield application


def held_in_escrow(conn, user, url="http://seller.test/test-site/train-times") -> int:
    """A payment of this user's agent in escrow while Sentinel is unsure of what the seller delivered."""
    attempt_id = payments.authorize(conn, user["agent"], "train times", Quote(url, DATACO, 300_000, ASSET, NETWORK,
                                                                               300))
    ledger.hold_attempt(conn, attempt_id, INFO, "0x" + format(attempt_id, "064x"), 2_000_001_800, 2_000_003_600,
                        "0xauth")
    ledger.record_delivery(conn, attempt_id, b'{"route": "Tokyo weather"}', "application/json", "unsure",
                           "not_delivered", "It is a weather forecast, not train times.", True)
    return attempt_id


async def test_an_escrow_is_shown_read_only_with_its_deadline(browser, mailer, conn):
    alice = await signed_in(browser, mailer)
    user = user_of(conn, alice)
    held = held_in_escrow(conn, user)
    conn.execute("UPDATE payment_attempts SET ml_model = 'wallet_risk', ml_score = 0.2, ml_threshold = 0.5"
                 " WHERE id = %s", (held,))
    page = (await alice.get("/account/tasks")).text
    assert "In escrow" in page and "refunded automatically by" in page and "$0.30" in page
    assert "Wallet risk model 0.20 · escrow only at 0.50" in page
    for gone in ["Pay seller", "Refund me", "/account/tasks/capture", "/account/tasks/void", "waiting for you"]:
        assert gone not in page, gone
    assert "IN ESCROW" in (await alice.get("/account/payments")).text
    live = (await alice.get("/account/live")).text
    assert "Locked in escrow" in live and "Sentinel is checking the delivery" in live
    assert "Review delivery" not in live and "Pay or refund" not in live
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    assert "$0.30" not in (await bob.get("/account/tasks")).text


@pytest.mark.parametrize("path", ["/account/tasks/capture", "/account/tasks/void"])
async def test_the_owner_cannot_settle_an_escrow_by_hand(browser, mailer, conn, operator, path):
    alice = await signed_in(browser, mailer)
    held = held_in_escrow(conn, user_of(conn, alice))
    response = await alice.post(path, data={"csrf": await csrf(alice), "request_id": held})
    assert response.status_code in (403, 404, 405) and operator.calls == []
