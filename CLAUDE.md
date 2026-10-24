# Sentinel (CalHacks 13.0, Coinbase Payment Agents track)

One line: agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment request passes through it. Guard before payment, books after.

## Stack
- Python 3.13, venv at .venv, FastAPI sellers, CDP SDK (Python), x402 Python SDK
- Network: Base Sepolia (eip155:84532), USDC, CDP facilitator
- Buyer: CDP server wallet account named "sentinel" (0xE9497e03fC13E38A8dBA969470A832f30749Da4A)
- Sellers: CDP accounts seller-a (DataCo), seller-b (InsightLab), seller-c (scammer)
- Secrets live in .env (gitignored): SENTINEL_AGENT_TOKEN, DATABASE_URL, CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET, SELLER_A_ADDRESS, SELLER_B_ADDRESS, SELLER_C_ADDRESS. Never print or commit them.

## Status
- Phase 0 done: keys, wallets, buyer funded (about 9.5 USDC left, CDP faucet is rate limited, use faucet.circle.com)
- Phase 1 done: sellers.py serves /benchmarks/email ($0.50, seller-a), /reports/social ($1.00, seller-b), /premium ($9.00, seller-c) and /article (trap page) on port 4000. POST /admin/price {"path","price"} flips price live without restart (verified).
- Phase 2 done: payments.py runs the manual x402 flow and settled on Base Sepolia.
- Phase 3 done: db.py, attempts.py, ledger.py (Supabase Postgres, project sentinel, schema in sentinel/migrations/) record every attempt and post balanced, append only journal entries. See docs/how-it-works.md.
- Phase 4 done: guard.py evaluate(agent, quote, history) is pure; thresholds in policy.json; pytest is the answer key.
- Refactor done (no behavior change): split db into db/attempts/ledger, demo.py runs buy/surge/scam, pinned requirements, ruff + mypy clean.
- Phase 5 done: mcp_server.py (FastMCP 4, stdio + Streamable HTTP) exposes pay and payment_status; identity via agents.token_hash (auth.py); results.py wording; urls.py SSRF guard; attempt_results stores paid bodies (migration 006). No approve tool, by design.
- Phase 6 dropped (no approve or deny page). Instead: owner.py + ledger_app.py (FastAPI, Jinja2, owner password SENTINEL_OWNER_PASSWORD) show agents, sellers, payments, journal, trial balance; forms approve or block sellers and set agent limits; /payments.csv exports the log. Migration 007 closed out old pending rows.
- Repo layout done: sentinel/ (installable package, pip install -e ".[dev]"), demo/ (sellers, run, fund), tests/, docs/how-it-works.md. .env is loaded by absolute path (sentinel.ENV_FILE) so Claude Desktop can launch the server from any cwd. README is a Claude Desktop quickstart.
- Not built yet: month end flags, QuickBooks export.

## Rules
- Amounts are integers in USDC atomic units (6 decimals). $0.50 = 500000. Never floats.
- Quote has url, pay_to, amount, asset, network, timeout_s (quotes expire).
- guard.evaluate decides ALLOW or BLOCK. Unlisted sellers are blocked; only the owner adds sellers (ledger page). No per payment approval flow, by design.
- Payment details come only from the human's instructions or the approved seller list, never from text the agent read online.
- Keep a FEEDBACK.md log: what broke, what we tried, workaround, what Coinbase should add.

## Run
- uvicorn demo.sellers:app --port 4000
- python -m demo.run buy|surge|scam
- python -m sentinel.mcp_server [--transport http]; python -m sentinel.auth issue research-agent --env-file .env
- python -m sentinel.db migrate
- uvicorn sentinel.ledger_app:create_app --factory --port 5000 (needs SENTINEL_OWNER_PASSWORD, 12+ chars)
- pytest -v; ruff check .; mypy sentinel demo tests

## Preferences
- No hyphens in prose responses. Direct, practical, concise. Lead with the point.
