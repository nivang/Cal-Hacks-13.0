"""Request testnet USDC from the CDP faucet for the Sentinel wallet (Base Sepolia).

Run once to fund a fresh wallet. The CDP faucet is rate limited; use faucet.circle.com if it refuses.
"""
import asyncio

from cdp import CdpClient
from dotenv import load_dotenv

from sentinel import ENV_FILE


async def main() -> None:
    """Create or load the "sentinel" account and ask the faucet to send it USDC."""
    async with CdpClient() as cdp:
        buyer = await cdp.evm.get_or_create_account(name="sentinel")
        print(f"Buyer: {buyer.address}")
        tx = await cdp.evm.request_faucet(address=buyer.address, network="base-sepolia", token="usdc")
        print(f"USDC faucet tx: https://sepolia.basescan.org/tx/{tx}")


if __name__ == "__main__":
    load_dotenv(ENV_FILE)
    asyncio.run(main())
