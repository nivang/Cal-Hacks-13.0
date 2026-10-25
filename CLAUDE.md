# Sentinel (CalHacks 13.0, Coinbase Payment Agents track)

One line: agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment request passes through it. Guard before payment, books after.

## Stack
- Python 3.13, venv at .venv, FastAPI sellers, CDP SDK (Python), x402 Python SDK
- Network: Base Sepolia (eip155:84532), USDC, CDP facilitator
- Buyers: each signed in user has their own CDP server account, named by users.id (uuid). The shared demo wallet is the CDP account "sentinel" (0xE9497e03fC13E38A8dBA969470A832f30749Da4A), demo.DEMO_ACCOUNT, used only from demo/
- Sellers: CDP accounts seller-a (DataCo), seller-b (InsightLab), seller-c (scammer)
- Secrets live in .env (gitignored): DATABASE_URL, CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET, SESSION_SECRET, TOKEN_HASH_SECRET, ANTHROPIC_API_KEY, RESEND_API_KEY, SENTINEL_OWNER_PASSWORD, SELLER_A_ADDRESS, SELLER_B_ADDRESS, SELLER_C_ADDRESS. Config (not secret): PUBLIC_BASE_URL, MAIL_FROM. Never print or commit them.

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
- Per user onboarding done: python -m sentinel.mcp_server serves the sign in site, the OAuth server and /mcp on one origin (PUBLIC_BASE_URL, default http://127.0.0.1:8000), HTTP only. Users sign in with an email magic link (Resend; printed to stderr on localhost); the first sign in creates their own CDP account (named by users.id), agent user-<uuid> and cash account 1010-<agent id>. Claude connects by DCR and OAuth (sentinel/web/oauth.py on FastMCP's OAuthProvider, scope pay, 1 h access tokens, rotating refresh, 90 day grants); every pay call opens that user's wallet (lookup only) and checks its address. Guard limits and price history are per user. Migration 008 adds users, magic_links, oauth_clients, oauth_codes, oauth_tokens and agents.cash_account_id; migrations are tracked (baseline 001 to 006, 007 reruns). sentinel/auth.py, stdio and SENTINEL_AGENT_TOKEN are gone (agents.token_hash stays, unused). The server refuses to start on missing env, bad settings or pending migrations. See docs/onboarding.md.
- Account pages done: a signed in user does everything from /account (Overview, /account/payments, /account/rules, /account/books, /account/payments.csv): approve or block their own sellers, set their own cap and budget, read payments and books, download the CSV. Seller lists are the operator's (sellers.agent_id null, applies to every agent, owner.py and the ledger page) plus a personal list per user (web/books.py); any applicable blocked row wins, then approved, else not listed. Users never change operator rows. Migration 009 adds sellers.agent_id with two partial unique indexes. In dev mode the Rules page offers the demo sellers DataCo and InsightLab (env SELLER_A_ADDRESS, SELLER_B_ADDRESS), never the scammer. The ledger page is for the operator only and moved to port 5001 (macOS uses 5000 for AirPlay, which answers 403).
- Intent binding done: every MCP payment belongs to a task. start_task(task, budget_usd?) has Claude Haiku 5.5 (judge.py, anthropic SDK 2.3.0) read the owner's words into a mandate (mandates table, migration 010), live at once but capped at $1 until the owner confirms it on /account/tasks. pay(task_id, url, reason, found_at?) runs the guard first, then payments.review_intent: tier 0 rules (intent.py, pure: revoked or expired, task budget, rate, loop, Allow once, split, per call, drift), tier 1 judge.classify (cached per task, seller and URL), tier 2 judge.decide. Outcomes ALLOW, BLOCK or ESCALATE (status escalated, result ESCALATED, held for the owner; Allow once on the Tasks page writes an allowances row the next identical pay uses up). Seller text and the agent's reason reach the judge only as quoted JSON fields; any judge failure holds, never pays. The allowlist stays the hard rule. demo.run intent and held show it. Measured first look latency is about 1.2 to 2.6 s per model call; acceptable for now.
- Discovery done: find_services(task_id, query) and quote(task_id, url) preview what pay would decide without paying or recording (payments.preview, discovery.py; catalog in x402 Bazaar shape from SENTINEL_DISCOVERY_URL, default the local marketplace at :4000/discovery/resources). demo/sellers.py is a marketplace (trip data plus traps); tier 0 gained big_share (one purchase over 50% of the task budget is held). demo/agent.py runs a Claude API research agent (claude-opus-5-5, tool runner) over the same core with a printed trace. Agents start one task per owner request.
- Seller attack rules done: guard adds long_window (authorization valid > 10 min), lookalike (address poisoning: same first 6 and last 4 hex as an approved or own address), payee_changed (a URL already paid to another wallet); price_jump is per product (seller and URL). intent tier 0 adds injection (seller text addressed to AI agents), unlisted_url (claims the catalog, never listed), bait_switch (402 asks more or pays another wallet than the listing; listings_seen, migration 012), listing_changed (held). All deterministic, before any model call. Approving a seller checks Base Sepolia (chain.py) and asks for Approve anyway on a fresh or lookalike wallet. Payments page grouped by task.
- Wallet history done: chain.wallet_profile (Blockscout explorer, RPC fallback; cached 1 h in wallet_profiles, migration 013). Approval asks for Approve anyway on a flagged, unused, new (< 24 h) or lookalike wallet; the Rules page shows each seller's history. At payment: flagged_wallet (guard, BLOCK), unused_wallet (intent tier 0, held). Demo sellers are under a day old, so they show as new on Rules but are not held.
- Escrow done: x402 auth-capture (escrow.py; no SDK support, so hashing, nonce, ReceiveWithAuthorization and calldata are ours, checked against the contract's getHash). AuthCaptureEscrow v1.1 0xf96815976523E00e65Be8f34cA5e64b4f41EB19c on Base Sepolia; Sentinel is the operator (CDP account sentinel-operator, 0xF358117Bd126666eBB2196906D21ddB58fB50f17, pays gas in testnet ETH). select_requirement prefers an escrow offer passing escrow.check_terms, else exact. After authorize, delivery.py rules (fail: void) then judge.check_delivery (delivered: capture; else held on the Tasks page, Pay seller or Refund me). Sweeper every 60 s retries and voids holds 5 min before the deadline. Ledger: Dr 1050-<agent id> on hold, then Dr expense or Dr cash; journal entries unique per attempt and step (migration 014). Exact deliveries are checked and recorded; 2 failures hold the seller (bad_deliveries). Demo sellers DataCo and InsightLab offer escrow plus exact (demo/escrow_seller.py, Bazaar output fields); /travel/restaurants voids, /travel/train-times holds. demo.run escrow|void|escrow-held. Verified live on Base Sepolia.
- Next: pay fetch hardening (IP pinning, response cap, deadline) before opening sign up to untrusted users.
- Not built yet: month end flags, QuickBooks export, escrow refunds after capture.

## Rules
- Amounts are integers in USDC atomic units (6 decimals). $0.50 = 500000. Never floats.
- Quote has url, pay_to, amount, asset, network, timeout_s (quotes expire).
- guard.evaluate decides ALLOW or BLOCK. Unlisted sellers are blocked; the operator's list (ledger page) and each user's own list (account page) decide. No general per payment approval flow, by design; the only exception is intent binding's hold (ESCALATED, Allow once on the Tasks page).
- Every MCP payment needs a task (start_task with the owner's own words). Intent binding runs only after the guard allows and can only block or hold. The agent never confirms tasks or allows held payments; only the signed in owner does.
- Payment details come only from the human's instructions or the approved seller list, never from text the agent read online.
- No default wallet; the shared test wallet lives only in demo/.
- Migrations only through python -m sentinel.db migrate.
- Keep a FEEDBACK.md log: what broke, what we tried, workaround, what Coinbase should add.

## Run
- uvicorn demo.sellers:app --port 4000
- python -m demo.run buy|surge|scam|intent|held|escrow|void|escrow-held
- python -m demo.escrow_spike capture|void (one raw escrow round trip; creates and funds the operator)
- python -m demo.agent "I'm researching a 5 day trip to Tokyo, keep data costs under $3"
- python -m sentinel.db migrate (after every pull)
- python -m sentinel.mcp_server [--host 127.0.0.1] [--port 8000] (site, OAuth and MCP at /mcp); then claude mcp add --transport http sentinel http://localhost:8000/mcp and /mcp
- uvicorn sentinel.ledger_app:create_app --factory --port 5001 (operator only; needs SENTINEL_OWNER_PASSWORD, 12+ chars; not 5000, macOS AirPlay uses it)
- pytest -v; ruff check .; mypy sentinel demo tests

## Preferences
- No hyphens in prose responses. Direct, practical, concise. Lead with the point.
