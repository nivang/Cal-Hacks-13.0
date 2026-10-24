"""Command line demo of the three guard moments against the local test sellers (sellers.py).

    python demo.py buy     clean $0.50 purchase from approved DataCo: ALLOW, settles on Base Sepolia
    python demo.py surge   DataCo suddenly quotes 4x the usual price: BLOCK (price_jump)
    python demo.py scam    a quote from an unlisted seller, as an injected page would push: BLOCK (seller)

Also seeds the approved sellers and the opening balance on first run. Real testnet payments, no simulation.
"""
import asyncio
import os
import sys

import httpx
from cdp import CdpClient
from dotenv import load_dotenv

import db
import ledger
from payments import NETWORK, USDC_BASE_SEPOLIA, PaymentRejected, pay_for

SELLERS_URL = "http://localhost:4000"
USDC_BALANCE_OF = "0x70a08231"  # ERC20 balanceOf(address) selector
BASE_SEPOLIA_RPC = "https://sepolia.base.org"


async def usdc_balance(address: str) -> int:
    """Read an address's USDC balance on Base Sepolia, in atomic units."""
    data = USDC_BALANCE_OF + address.lower().removeprefix("0x").rjust(64, "0")
    call = {"to": USDC_BASE_SEPOLIA, "data": data}
    async with httpx.AsyncClient() as http:
        r = await http.post(
            BASE_SEPOLIA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, "latest"]},
        )
    return int(r.json()["result"], 16)


async def bootstrap_books(conn: db.Connection, wallet_address: str) -> None:
    """Seed the approved sellers from env and book the wallet's current USDC as owner funding, once."""
    approved = {"DataCo": "SELLER_A_ADDRESS", "InsightLab": "SELLER_B_ADDRESS"}
    db.seed_sellers(conn, [
        {"name": name, "pay_to": os.environ[var], "network": NETWORK, "asset": USDC_BASE_SEPOLIA, "status": "approved"}
        for name, var in approved.items()
    ])
    if not ledger.has_entries(conn):
        balance = await usdc_balance(wallet_address)
        ledger.post_funding(conn, balance, "Opening balance: USDC already in sentinel wallet")


async def set_price(path: str, price: str) -> None:
    """Change a test seller's price live through its admin endpoint (demo fixture only)."""
    async with httpx.AsyncClient() as http:
        await http.post(f"{SELLERS_URL}/admin/price", json={"path": path, "price": price})


async def run(scenario: str) -> None:
    """Run one demo scenario and print what the guard decided, then the trial balance."""
    paths = {"buy": "/benchmarks/email", "surge": "/benchmarks/email", "scam": "/premium"}
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(name="sentinel")
        with db.connection() as conn:
            db.init(conn)
            await bootstrap_books(conn, account.address)
        if scenario == "surge":
            await set_price("/benchmarks/email", "$2.00")
        try:
            result = await pay_for(f"{SELLERS_URL}{paths[scenario]}", account, reason=f"demo {scenario}")
            print(f"PAID HTTP {result.status} {result.body}")
            print(f"https://sepolia.basescan.org/tx/{result.tx_hash}")
        except PaymentRejected as e:
            print(f"BLOCKED  {e}")
        finally:
            if scenario == "surge":
                await set_price("/benchmarks/email", "$0.50")

    with db.connection() as conn:
        for row in ledger.trial_balance(conn):
            print(f"{row['code']} {row['name']:<30} Dr {row['debit']:>9} Cr {row['credit']:>9}")


if __name__ == "__main__":
    load_dotenv()
    if len(sys.argv) != 2 or sys.argv[1] not in ("buy", "surge", "scam"):
        sys.exit(__doc__)
    try:
        asyncio.run(run(sys.argv[1]))
    finally:
        db.close_pool()
