"""One escrow round trip on Base Sepolia, outside the payment flow: hold $0.10 from the demo wallet, then pay CorpData
(capture) or give it back (void). Proves the signing domain, the nonce, the calldata and the operator's gas.

Creates the operator account (SENTINEL_OPERATOR_ACCOUNT, default sentinel-operator) on first run and asks the CDP
faucet for ETH when it has none.
Run with: python -m demo.escrow_spike capture|void
"""
import asyncio
import os
import sys
import time

import httpx
from cdp import CdpClient
from dotenv import load_dotenv

from demo import DEMO_ACCOUNT
from sentinel import ENV_FILE, chain, escrow, wallets

AMOUNT = 100_000  # $0.10
TX = "https://sepolia.basescan.org/tx/"


async def eth_balance(address: str) -> int:
    """The operator's ETH, in wei."""
    async with httpx.AsyncClient(timeout=chain.TIMEOUT_S) as session:
        return await chain._call(session, "eth_getBalance", [address, "latest"])


async def fund_operator(address: str) -> None:
    """Ask the CDP faucet for ETH when the operator has none, and wait until it arrives."""
    if await eth_balance(address) > 0:
        return
    async with CdpClient() as cdp:
        tx = await cdp.evm.request_faucet(address=address, network=escrow.CDP_NETWORK, token="eth")
    print(f"ETH faucet: {TX}{tx}")
    for _ in range(60):
        if await eth_balance(address) > 0:
            return
        await asyncio.sleep(2)
    raise SystemExit("The operator still has no ETH; fund it and run again.")


async def main(action: str) -> None:
    async with escrow.open_operator(create=True) as operator:
        print(f"Operator: {operator.address}")
        await fund_operator(operator.address)
        print(f"Operator ETH: {await eth_balance(operator.address) / 1e18:.6f}")
        async with wallets.cdp_wallet(DEMO_ACCOUNT) as buyer:
            now = int(time.time())
            info = escrow.PaymentInfo(
                operator=operator.address, payer=buyer.address, receiver=os.environ["SELLER_A_ADDRESS"],
                token=escrow.USDC, max_amount=AMOUNT, pre_approval_expiry=now + 300,
                authorization_expiry=now + 3600, refund_expiry=now + 7200, salt=escrow.new_salt(),
            )
            payment = escrow.info_hash(info)
            assert payment == await escrow.onchain_hash(info), "local hash differs from the contract's getHash"
            seller_before = await chain.usdc_balance(info.receiver)
            buyer_before = await chain.usdc_balance(info.payer)
            signature = "0x" + buyer.sign_typed_data(*escrow.receive_typed_data(info)).hex()

        tx = await operator.authorize(info, signature)
        print(f"authorize: {TX}{tx}")
        print(f"  held: {await escrow.payment_state(payment)}")
        tx = await (operator.capture(info, AMOUNT) if action == "capture" else operator.void(info))
        print(f"{action}: {TX}{tx}")
        print(f"  after: {await escrow.payment_state(payment)}")
        print(f"Seller USDC change: {await chain.usdc_balance(info.receiver) - seller_before}")
        print(f"Buyer USDC change: {await chain.usdc_balance(info.payer) - buyer_before}")


if __name__ == "__main__":
    load_dotenv(ENV_FILE)
    if len(sys.argv) != 2 or sys.argv[1] not in ("capture", "void"):
        raise SystemExit("usage: python -m demo.escrow_spike capture|void")
    asyncio.run(main(sys.argv[1]))
