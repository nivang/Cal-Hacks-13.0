"""Read only lookups on Base Sepolia: does this wallet look like a real, established seller?

wallet_profile asks the Blockscout explorer (no key) for an address's activity, when it was first seen, and whether
the explorer flags it as a scam; if the explorer cannot be reached it falls back to the public RPC node, which can
only say whether the wallet was ever used. Two callers:
  - the account Rules page shows each seller's history.
  - every payment (payments.payee_profile, cached for an hour in wallet_profiles): a flagged payee is blocked
    (guard rule flagged_wallet) and one with no activity at all is blocked unless paid through escrow (intent rule
    unused_wallet).
"""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from sentinel.db import Connection

RPC_URL = "https://sepolia.base.org"
EXPLORER_API = "https://base-sepolia.blockscout.com/api/v2"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # the same token as payments.USDC_BASE_SEPOLIA
NEW_WALLET = timedelta(hours=24)  # first seen more recently than this: a new wallet
PAGE = 50  # Blockscout's page size: a wallet with more activity than one page is established
CACHE_TTL = timedelta(hours=1)
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


@dataclass(frozen=True)
class WalletProfile:
    """What the explorer knows about one address. first_seen is None when unknown or when it has a long history."""

    transactions: int
    token_transfers: int
    first_seen: datetime | None
    flagged: bool  # the explorer marks it as a scam or gives it a bad reputation
    source: str  # "explorer" or "rpc" (the fallback: activity only, no age or flag)

    @property
    def unused(self) -> bool:
        """Never sent or received anything."""
        return self.transactions == 0 and self.token_transfers == 0

    def is_new(self, now: datetime | None = None) -> bool:
        """First seen within NEW_WALLET."""
        if self.first_seen is None:
            return False
        return (now or datetime.now(timezone.utc)) - self.first_seen < NEW_WALLET

    def summary(self, now: datetime | None = None) -> str:
        """One line for the owner, e.g. "32 token transfers, first seen 17 hours ago"."""
        if self.flagged:
            return "flagged as a scam by the Base Sepolia explorer"
        if self.unused:
            return "no activity on Base Sepolia"
        parts = [f"{self.token_transfers} token transfers", f"{self.transactions} transactions sent"]
        if self.first_seen is not None:
            hours = int(((now or datetime.now(timezone.utc)) - self.first_seen).total_seconds() // 3600)
            parts.append(f"first seen {hours} hours ago" if hours < 48 else f"first seen {hours // 24} days ago")
        elif self.transactions + self.token_transfers >= PAGE:
            parts.append("long history")
        return ", ".join(parts)


def _when(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _explorer(session: httpx.AsyncClient, path: str) -> Any:
    response = await session.get(f"{EXPLORER_API}/{path}")
    response.raise_for_status()
    return response.json()


async def wallet_profile(address: str) -> WalletProfile:
    """The address's activity, age and scam flag from the explorer, else activity from the RPC node.

    Raises ChainError when neither can be read.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as session:
            info = await _explorer(session, f"addresses/{address}")
            counters = await _explorer(session, f"addresses/{address}/counters")
            transactions = int(counters.get("transactions_count") or 0)
            transfers = int(counters.get("token_transfers_count") or 0)
            first_seen = None
            if 0 < transactions + transfers < PAGE:  # small history: the oldest item on the first page is the first
                stamps = []
                for path in (f"addresses/{address}/token-transfers", f"addresses/{address}/transactions"):
                    items = (await _explorer(session, path)).get("items") or []
                    stamps += [t for t in (_when(item.get("timestamp")) for item in items) if t is not None]
                first_seen = min(stamps) if stamps else None
            flagged = bool(info.get("is_scam")) or info.get("reputation") not in (None, "ok")
            return WalletProfile(transactions, transfers, first_seen, flagged, "explorer")
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    history = await wallet_history(address)
    return WalletProfile(history.tx_count, 0 if history.unused else 1, None, False, "rpc")


async def cached_profile(conn: Connection, address: str) -> WalletProfile | None:
    """The address's profile, from the wallet_profiles cache when younger than CACHE_TTL, else fetched and stored.

    None when it cannot be read: the payment rules that use it then do not fire (the owner already approved this
    address, and approval checked it).
    """
    key = address.lower()
    row = conn.execute(
        "SELECT profile FROM wallet_profiles WHERE address = %s AND checked_at > now() - make_interval(secs => %s)",
        (key, int(CACHE_TTL.total_seconds())),
    ).fetchone()
    if row is not None:
        data = dict(row["profile"])
        data["first_seen"] = _when(data["first_seen"]) if data.get("first_seen") else None
        return WalletProfile(**data)
    try:
        profile = await wallet_profile(address)
    except ChainError:
        return None
    stored = {**asdict(profile), "first_seen": profile.first_seen.isoformat() if profile.first_seen else None}
    conn.execute(
        "INSERT INTO wallet_profiles (address, profile) VALUES (%s, %s::jsonb) ON CONFLICT (address) DO UPDATE"
        " SET profile = excluded.profile, checked_at = now()",
        (key, json.dumps(stored)),
    )
    return profile
