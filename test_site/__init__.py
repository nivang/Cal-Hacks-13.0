"""The test site: Malicious Marketplace, an x402 marketplace for AI agents to try Sentinel against. A fixture, not
part of Sentinel.

app.py serves pay per call APIs under PREFIX on port 4000 (uvicorn test_site.app:app --port 4000), listed in
services.json and answered from companies.json and data.py, from the seller wallets in SELLER_A_ADDRESS (CorpData),
SELLER_B_ADDRESS (OpenWire) and SELLER_C_ADDRESS (PremiumIntel, a scammer), plus traps: off task services, an unlisted
seller, a free page with hidden instructions for AI agents, sellers attacking the buyer, and escrow sellers that
deliver badly. storefront/ is the marketplace page for people (React, built to storefront/dist); agents read the JSON
catalog. escrow_seller.py is the seller side of escrow. Sentinel knows none of this; point it here with
SENTINEL_DISCOVERY_URL.
"""
import hashlib

PREFIX = "/test-site"
SHOP_NAME = "Malicious Marketplace"
# QuickScrape's wallet: an address that has never been used on Base Sepolia, so Sentinel holds payments to it.
FRESH_WALLET = "0x" + hashlib.sha256(b"sentinel demo: a wallet that has never been used").hexdigest()[:40]
