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


# The explorer profile


def explorer(monkeypatch, address, info, counters, transfers=(), transactions=()):
    """A fake Blockscout answering for one address."""
    pages = {
        f"/api/v2/addresses/{address}": info,
        f"/api/v2/addresses/{address}/counters": counters,
        f"/api/v2/addresses/{address}/token-transfers": {"items": [{"timestamp": t} for t in transfers]},
        f"/api/v2/addresses/{address}/transactions": {"items": [{"timestamp": t} for t in transactions]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(request.url.path)
        return httpx.Response(200, json=page) if page is not None else httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(chain.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


REAL_PROFILE = chain.wallet_profile
NOW = chain.datetime(2026, 10, 24, 22, 0, tzinfo=chain.timezone.utc)


async def test_a_small_history_gives_the_first_seen_time(monkeypatch):
    explorer(monkeypatch, ADDRESS, {"is_scam": False, "reputation": "ok"},
             {"transactions_count": "1", "token_transfers_count": "3"},
             transfers=["2026-10-24T21:00:00Z", "2026-10-24T20:00:00.000000Z"], transactions=["2026-10-24T20:30:00Z"])
    profile = await REAL_PROFILE(ADDRESS)
    assert (profile.transactions, profile.token_transfers, profile.flagged) == (1, 3, False)
    assert profile.first_seen == chain.datetime(2026, 10, 24, 20, 0, tzinfo=chain.timezone.utc)
    assert profile.is_new(NOW) and not profile.unused
    assert profile.summary(NOW) == "3 token transfers, 1 transactions sent, first seen 2 hours ago"


async def test_a_long_history_is_established_and_a_flag_is_kept(monkeypatch):
    explorer(monkeypatch, ADDRESS, {"is_scam": True}, {"transactions_count": "0", "token_transfers_count": "400"})
    profile = await REAL_PROFILE(ADDRESS)
    assert profile.first_seen is None and not profile.is_new(NOW) and profile.flagged
    assert profile.summary(NOW) == "flagged as a scam by the Base Sepolia explorer"


async def test_a_bad_reputation_counts_as_flagged(monkeypatch):
    explorer(monkeypatch, ADDRESS, {"is_scam": False, "reputation": "scam"},
             {"transactions_count": "0", "token_transfers_count": "0"})
    profile = await REAL_PROFILE(ADDRESS)
    assert profile.flagged and profile.unused


async def test_without_the_explorer_the_rpc_still_says_whether_it_was_used(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "base-sepolia.blockscout.com":
            return httpx.Response(503)
        return httpx.Response(200, json={"result": "0x0"})

    real = httpx.AsyncClient
    monkeypatch.setattr(chain.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(chain, "wallet_history", REAL_HISTORY)
    profile = await REAL_PROFILE(ADDRESS)
    assert profile.unused and profile.source == "rpc" and not profile.flagged


def test_the_usdc_address_matches_the_one_payments_pays_in():
    from sentinel import payments
    assert chain.USDC_BASE_SEPOLIA == payments.USDC_BASE_SEPOLIA
