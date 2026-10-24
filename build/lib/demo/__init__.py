"""Demo fixtures for Sentinel: the local test sellers, the guard demo and the faucet script.

DEMO_ACCOUNT is the only home of the shared test wallet: the CDP server account the demo pays from and the faucet
funds. Sentinel itself has no default wallet; every real user signs with their own account, named by their uuid
(wallets.open_user_wallet), so nothing outside demo/ may name this one.
"""

DEMO_ACCOUNT = "sentinel"
