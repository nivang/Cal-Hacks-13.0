# Sentinel (CalHacks 13.0, Coinbase Payment Agents track)

One line: agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment request passes through it. Guard before payment, books after.

## Stack
- Python 3.13, venv at .venv, FastAPI sellers, CDP SDK (Python), x402 Python SDK
- Network: Base Sepolia (eip155:84532), USDC, CDP facilitator
- Buyers: each signed in user has their own CDP server account, named by users.id (uuid). The shared demo wallet is the CDP account "sentinel" (0xE9497e03fC13E38A8dBA969470A832f30749Da4A), demo.DEMO_ACCOUNT, used only from demo/
- Sellers: CDP accounts seller-a (DataCo), seller-b (InsightLab), seller-c (scammer)
- Secrets live in .env (gitignored): DATABASE_URL, CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET, SESSION_SECRET, TOKEN_HASH_SECRET, RESEND_API_KEY, SENTINEL_OWNER_PASSWORD, SELLER_A_ADDRESS, SELLER_B_ADDRESS, SELLER_C_ADDRESS. Config (not secret): PUBLIC_BASE_URL, MAIL_FROM. Never print or commit them.

## Status
- Phase 0 done: keys, wallets, buyer funded (about 9.5 USDC left, CDP faucet is rate limited, use faucet.circle.com)
- Phase 1 done: sellers.py serves /benchmarks/email ($0.50, seller-a), /reports/social ($1.00, seller-b), /premium ($9.00, seller-c) and /article (trap page) on port 4000. POST /admin/price {"path","price"} flips price live without restart (verified).
- Phase 2 done: payments.py runs the manual x402 flow and settled on Base Sepolia.
- Phase 3 done: db.py, attempts.py, ledger.py (Supabase Postgres, project sentinel, schema in sentinel/migrations/) record every attempt and post balanced, append only journal entries. See docs/how-it-works.md.
- Phase 4 done: guard.py evaluate(agent, quote, history) is pure; thresholds in policy.json; pytest is the answer key.
- Refactor done (no behavior change): split db into db/attempts/ledger, demo.py runs buy/surge/scam, pinned requirements, ruff + mypy clean.
- Phase 5 done: mcp_server.py (FastMCP 4, stdio + Streamable HTTP) exposes pay and payment_status; identity via agents.token_hash (auth.py); results.py wording; urls.py SSRF guard; attempt_results stores paid bodies (migration 006). No approve tool, by design.
- Phase 6 dropped (no approve or deny page). Instead: owner.py + ledger_app.py (FastAPI, Jinja2, owner password SENTINEL_OWNER_PASSWORD) show agents, sellers, payments, journal, trial balance; forms approve or block sellers and set agent limits; /payments.csv exports the log. Migration 007 closed out old pending rows.
- Repo layout done: sentinel/ (installable package, pip install -e ".[dev]"; sentinel/web/ holds the sign in site and OAuth server), demo/ (sellers, run, fund, DEMO_ACCOUNT), tests/, docs/how-it-works.md, docs/onboarding.md. .env is loaded by absolute path (sentinel.ENV_FILE) so the server runs from any cwd. README is a quickstart (run the server, sign in, connect Claude); docs/onboarding.md covers Desktop, claude.ai, Inspector and deploy.
- Wallet seam done: wallets.py opens the signer (SENTINEL_WALLET, default cdp, the only one built in). payments.py and mcp_server.py only see an x402 ClientEvmSigner; a new wallet is one opener plus one WALLETS entry. No raw private key mode, by design.
- Per user onboarding done: python -m sentinel.mcp_server serves the sign in site, the OAuth server and /mcp on one origin (PUBLIC_BASE_URL, default http://127.0.0.1:8000), HTTP only. Users sign in with an email magic link (Resend; printed to stderr on localhost); the first sign in creates their own CDP account (named by users.id), agent user-<uuid> and cash account 1010-<agent id>. Claude connects by DCR and OAuth (sentinel/web/oauth.py on FastMCP's OAuthProvider, scope pay, 1 h access tokens, rotating refresh, 90 day grants); every pay call opens that user's wallet (lookup only) and checks its address. Guard limits and price history are per user; the seller list stays global. Migration 008 adds users, magic_links, oauth_clients, oauth_codes, oauth_tokens and agents.cash_account_id; migrations are tracked (baseline 001 to 006, 007 reruns). sentinel/auth.py, stdio and SENTINEL_AGENT_TOKEN are gone (agents.token_hash stays, unused). The server refuses to start on missing env, bad settings or pending migrations. See docs/onboarding.md.
- Next: pay fetch hardening (IP pinning, response cap, deadline) before opening sign up to untrusted users.
- Not built yet: month end flags, QuickBooks export.

## Rules
- Amounts are integers in USDC atomic units (6 decimals). $0.50 = 500000. Never floats.
- Quote has url, pay_to, amount, asset, network, timeout_s (quotes expire).
- guard.evaluate decides ALLOW or BLOCK. Unlisted sellers are blocked; only the owner adds sellers (ledger page). No per payment approval flow, by design.
- Payment details come only from the human's instructions or the approved seller list, never from text the agent read online.
- No default wallet; the shared test wallet lives only in demo/.
- Migrations only through python -m sentinel.db migrate.
- Keep a FEEDBACK.md log: what broke, what we tried, workaround, what Coinbase should add.

## Run
- uvicorn demo.sellers:app --port 4000
- python -m demo.run buy|surge|scam
- python -m sentinel.db migrate (after every pull)
- python -m sentinel.mcp_server [--host 127.0.0.1] [--port 8000] (site, OAuth and MCP at /mcp); then claude mcp add --transport http sentinel http://localhost:8000/mcp and /mcp
- uvicorn sentinel.ledger_app:create_app --factory --port 5000 (needs SENTINEL_OWNER_PASSWORD, 12+ chars)
- pytest -v; ruff check .; mypy sentinel demo tests

## Preferences
- No hyphens in prose responses. Direct, practical, concise. Lead with the point.
