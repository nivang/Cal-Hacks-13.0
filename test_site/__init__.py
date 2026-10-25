"""The test site: a small x402 marketplace to try Sentinel against. A fixture, not part of Sentinel.

app.py serves every product under PREFIX on port 4000 (uvicorn test_site.app:app --port 4000), from the seller
wallets in SELLER_A_ADDRESS (DataCo), SELLER_B_ADDRESS (InsightLab) and SELLER_C_ADDRESS (a scammer), plus traps:
off task products, an unlisted seller, pages with hidden instructions for AI agents, sellers attacking the buyer, and
escrow sellers that deliver badly. escrow_seller.py is the seller side of escrow. Sentinel knows none of this; point
it here with SENTINEL_DISCOVERY_URL.
"""
import hashlib

PREFIX = "/test-site"
# PocketWiFi's wallet: an address that has never been used on Base Sepolia, so Sentinel holds payments to it.
FRESH_WALLET = "0x" + hashlib.sha256(b"sentinel demo: a wallet that has never been used").hexdigest()[:40]
