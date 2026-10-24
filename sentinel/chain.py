"""Read only lookups on Base Sepolia: has this wallet ever been used?

The account Rules page (and the operator's ledger page) call wallet_history before approving a seller address: a
wallet with no transactions and no USDC or ETH may have just been generated, which is how a scam seller sets up, so
the owner is asked to confirm. Public RPC, no keys. Never used to decide a payment.
"""
from dataclasses import dataclass

import httpx

from sentinel.payments import USDC_BASE_SEPOLIA

RPC_URL = "https://sepolia.base.org"
TIMEOUT_S = 5.0
BALANCE_OF = "0x70a08231"  # ERC20 balanceOf(address) selector


class ChainError(Exception):
    """The chain could not be read."""


@dataclass(frozen=True)
class WalletHistory:
    """What Base Sepolia knows about one address."""

    tx_count: int  # transactions it has sent
    eth: int  # wei
    usdc: int  # atomic units

    @property
    def unused(self) -> bool:
        """True when it never sent a transaction and holds nothing: possibly a wallet generated a moment ago."""
        return self.tx_count == 0 and self.eth == 0 and self.usdc == 0


async def _call(session: httpx.AsyncClient, method: str, params: list[object]) -> int:
    """One JSON-RPC call whose result is a hex quantity."""
    response = await session.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    result = response.json().get("result")
    if not isinstance(result, str):
        raise ChainError(f"{method} returned no result")
    return int(result, 16) if result not in ("0x", "") else 0


async def usdc_balance(address: str) -> int:
    """An address's USDC balance on Base Sepolia, in atomic units."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as session:
        return await _usdc(session, address)


async def _usdc(session: httpx.AsyncClient, address: str) -> int:
    data = BALANCE_OF + address.lower().removeprefix("0x").rjust(64, "0")
    return await _call(session, "eth_call", [{"to": USDC_BASE_SEPOLIA, "data": data}, "latest"])


async def wallet_history(address: str) -> WalletHistory:
    """Transactions sent, ETH and USDC held by an address. Raises ChainError when the RPC cannot be read."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as session:
            return WalletHistory(
                tx_count=await _call(session, "eth_getTransactionCount", [address, "latest"]),
                eth=await _call(session, "eth_getBalance", [address, "latest"]),
                usdc=await _usdc(session, address),
            )
    except (httpx.HTTPError, ValueError) as e:
        raise ChainError(type(e).__name__) from e
