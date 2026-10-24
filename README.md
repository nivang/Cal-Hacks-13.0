# Sentinel

Agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment is checked against your rules before it is paid and booked after. It pays in testnet USDC on Base Sepolia, so no real money moves. Design details: [docs/how-it-works.md](docs/how-it-works.md).

## You need

- Python 3.13, and Claude (Claude Code, Claude Desktop or claude.ai)
- A [Coinbase Developer Platform](https://portal.cdp.coinbase.com) API key and wallet secret, plus three EVM accounts named seller-a, seller-b, seller-c (copy their addresses)
- A [Supabase](https://supabase.com) project (copy its pooled connection string)

## Setup

```bash
git clone https://github.com/nivang/Cal-Hacks-13.0.git && cd Cal-Hacks-13.0
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # see below
python -m sentinel.db migrate      # builds the tables; run it again after every pull
```

In `.env` fill in the CDP keys, the seller addresses and `DATABASE_URL`, then for a local run:

- `PUBLIC_BASE_URL=http://localhost:8000`
- `SESSION_SECRET` and `TOKEN_HASH_SECRET`: two different values from `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `SENTINEL_ALLOW_LOCAL_URLS=1` (lets Sentinel pay the local test sellers)
- `RESEND_API_KEY` and `MAIL_FROM` can stay empty on localhost: sign in links print in the server's terminal.

## Quickstart: run, sign in, connect Claude

1. **Run the server and the test sellers.**

   ```bash
   python -m sentinel.mcp_server            # terminal 1: site, OAuth and MCP on http://localhost:8000
   uvicorn demo.sellers:app --port 4000     # terminal 2: test sellers
   ```

   It refuses to start, and says why, when a variable is missing or wrong or the database needs `python -m sentinel.db migrate`.

2. **Sign in.** Open http://localhost:8000, enter your email, and open the link printed in terminal 1. Your first sign in creates your own wallet; the account page shows its address. Fund it with Base Sepolia USDC at [faucet.circle.com](https://faucet.circle.com).

3. **Connect Claude.** In Claude Code:

   ```bash
   claude mcp add --transport http sentinel http://localhost:8000/mcp
   ```

   then run `/mcp`, authenticate, and click Allow in the browser. Ask: *"Buy http://localhost:4000/benchmarks/email, I need email benchmarks for the Q3 report."* Sellers must be approved first: run the demo below once (it approves DataCo and InsightLab), or approve them on the owner ledger page; otherwise the payment is BLOCKED (seller).

Claude Desktop, claude.ai, MCP Inspector and deploying to a public host: [docs/onboarding.md](docs/onboarding.md).

## Demo without Claude

`python -m demo.run buy` pays from the shared demo wallet (CDP account `sentinel`, created on first run; fund it with `python -m demo.fund` or at faucet.circle.com). It also approves the demo sellers and books the opening balance, and should print PAID and a basescan link.

## Owner ledger page

Set `SENTINEL_OWNER_PASSWORD` (12+ characters) in `.env`, then:

```bash
uvicorn sentinel.ledger_app:create_app --factory --port 5000    # open http://127.0.0.1:5000
```

This is where you approve or block sellers, set agent limits, read the books and download the payments CSV. The agent cannot reach it.

## Try the guard

```bash
python -m demo.run surge    # seller quotes 4x the usual price: BLOCKED
python -m demo.run scam     # unlisted seller: BLOCKED
```

## Troubleshooting

- The server will not start: read its message. It names every missing variable, every bad value, or the pending migrations (`python -m sentinel.db migrate`).
- No sign in email: on localhost the link is printed in the server's terminal.
- A form says "That request was refused": open the site on the exact host in `PUBLIC_BASE_URL` (`localhost` and `127.0.0.1` differ).
- BLOCKED (seller): that seller is not approved. Approve it on the ledger page.
- More: [docs/onboarding.md](docs/onboarding.md#troubleshooting).

## Development

```bash
pytest          # database tests use the "test" schema of your Supabase database
ruff check .
mypy sentinel demo tests
```
