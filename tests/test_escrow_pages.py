"""Tests for payments held in escrow on the account pages: the owner pays the seller or takes the money back.

Overrides the app fixture so it has an escrow operator (a fake that records calls); everything else is conftest's.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from conftest import ASSET, DATACO, NETWORK, form_value
from fastapi import FastAPI
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from test_account_pages import BOB_IP, csrf, signed_in, user_of
from test_escrow_flow import FakeOperator

from sentinel import attempts, ledger, payments
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
    """A payment of this user's agent held in escrow for the owner, with what the seller delivered."""
    attempt_id = payments.authorize(conn, user["agent"], "train times", Quote(url, DATACO, 300_000, ASSET, NETWORK,
                                                                               300))
    ledger.hold_attempt(conn, attempt_id, INFO, "0x" + format(attempt_id, "064x"), 2_000_001_800, 2_000_003_600,
                        "0xauth")
    ledger.record_delivery(conn, attempt_id, b'{"route": "Tokyo weather"}', "application/json", "unsure",
                           "not_delivered", "It is a weather forecast, not train times.", True)
    return attempt_id


async def test_the_owner_sees_a_held_delivery_and_pays_the_seller(browser, mailer, conn, operator):
    alice = await signed_in(browser, mailer)
    user = user_of(conn, alice)
    held = held_in_escrow(conn, user)
    page = await alice.get("/account/tasks")
    assert "Held in escrow" in page.text and "weather forecast, not train times" in page.text
    assert "Tokyo weather" in page.text  # what the seller sent, so the owner can judge it
    assert "IN ESCROW" in (await alice.get("/account/payments")).text
    response = await alice.post("/account/tasks/capture", data={"csrf": form_value(page.text, "csrf"),
                                                                 "request_id": held})
    assert response.status_code == 303 and response.headers["location"] == "/account/tasks?done=capture"
    assert operator.calls == ["capture"]
    assert attempts.get_attempt(conn, held, user["agent"])["status"] == "settled"
    assert "Held in escrow" not in (await alice.get("/account/tasks")).text


async def test_the_owner_refunds_a_held_delivery(browser, mailer, conn, operator):
    alice = await signed_in(browser, mailer)
    user = user_of(conn, alice)
    held = held_in_escrow(conn, user)
    response = await alice.post("/account/tasks/void", data={"csrf": await csrf(alice), "request_id": held})
    assert response.status_code == 303 and operator.calls == ["void"]
    assert attempts.get_attempt(conn, held, user["agent"])["status"] == "voided"
    assert "REFUNDED" in (await alice.get("/account/payments")).text


@pytest.mark.parametrize("path", ["/account/tasks/capture", "/account/tasks/void"])
async def test_nobody_can_settle_another_users_escrow(browser, mailer, conn, operator, path):
    alice = await signed_in(browser, mailer)
    held = held_in_escrow(conn, user_of(conn, alice))
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    assert (await bob.post(path, data={"csrf": await csrf(bob), "request_id": held})).status_code == 400
    assert operator.calls == []


async def test_a_chain_failure_leaves_it_held_and_says_try_again(browser, mailer, conn, operator):
    alice = await signed_in(browser, mailer)
    user = user_of(conn, alice)
    held = held_in_escrow(conn, user)
    operator.fail = {"capture"}
    response = await alice.post("/account/tasks/capture", data={"csrf": await csrf(alice), "request_id": held})
    assert response.status_code == 502 and "Try again in a minute" in response.text
    assert attempts.get_attempt(conn, held, user["agent"])["status"] == "escrowed"

