"""Tests for chain.py against a fake JSON-RPC endpoint. No network.

conftest's autouse fake_chain replaces chain.wallet_history for every other test; these keep the real one, taken at
import time, before that fixture runs.
"""
import json

import httpx
import pytest

from sentinel import chain

REAL_HISTORY = chain.wallet_history
ADDRESS = "0x" + "ab" * 20

pytestmark = pytest.mark.anyio


def rpc(monkeypatch, answers):
    """Answer each JSON-RPC method from answers (a hex string, or an httpx.Response to return as is)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        seen.append(method)
        answer = answers[method]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json={"result": answer})

    real = httpx.AsyncClient
    monkeypatch.setattr(chain.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


async def test_a_wallet_that_never_sent_or_held_anything_is_unused(monkeypatch):
    seen = rpc(monkeypatch, {"eth_getTransactionCount": "0x0", "eth_getBalance": "0x0", "eth_call": "0x"})
    history = await REAL_HISTORY(ADDRESS)
    assert history == chain.WalletHistory(0, 0, 0) and history.unused
    assert seen == ["eth_getTransactionCount", "eth_getBalance", "eth_call"]


async def test_a_wallet_that_only_received_usdc_is_not_unused(monkeypatch):
    rpc(monkeypatch, {"eth_getTransactionCount": "0x0", "eth_getBalance": "0x0", "eth_call": hex(9_800_000)})
    history = await REAL_HISTORY(ADDRESS)
    assert history.usdc == 9_800_000 and not history.unused


@pytest.mark.parametrize("broken", [httpx.Response(500), httpx.Response(200, json={"error": "nope"}),
                                    httpx.Response(200, content=b"not json")])
async def test_an_rpc_that_fails_raises_chain_error(monkeypatch, broken):
    rpc(monkeypatch, {"eth_getTransactionCount": broken, "eth_getBalance": "0x0", "eth_call": "0x0"})
    with pytest.raises(chain.ChainError):
        await REAL_HISTORY(ADDRESS)
