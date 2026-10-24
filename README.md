# Sentinel

Agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment is checked against your rules, and against the task you actually gave the agent, before it is paid and booked after. It pays in testnet USDC on Base Sepolia, so no real money moves. Design details: [docs/how-it-works.md](docs/how-it-works.md).

## You need

- Python 3.13, and Claude (Claude Code, Claude Desktop or claude.ai)
- A [Coinbase Developer Platform](https://portal.cdp.coinbase.com) API key and wallet secret, plus three EVM accounts named seller-a, seller-b, seller-c (copy their addresses)
- A [Supabase](https://supabase.com) project (copy its pooled connection string)
- An [Anthropic API key](https://console.anthropic.com): Claude reads each task and checks payments against it

## Setup

```bash
git clone https://github.com/nivang/Cal-Hacks-13.0.git && cd Cal-Hacks-13.0
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # see below
python -m sentinel.db migrate      # builds the tables; run it again after every pull
```

In `.env` fill in the CDP keys, the seller addresses, `DATABASE_URL` and `ANTHROPIC_API_KEY`, then for a local run:

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

2. **Sign in.** Open http://localhost:8000, enter your email, and open the link printed in terminal 1. Your first sign in creates your own wallet; the account page shows its address. Fund it with Base Sepolia USDC at [faucet.circle.com](https://faucet.circle.com) (the **Fund it** button), then open **Rules** on the account page and approve the sellers your agent may pay (locally, the Rules page offers DataCo and InsightLab with one click).

3. **Connect Claude.** In Claude Code:

   ```bash
   claude mcp add --transport http sentinel http://localhost:8000/mcp
   ```

   then run `/mcp`, authenticate, and click Allow in the browser. Ask: *"Buy http://localhost:4000/benchmarks/email, I need email benchmarks for the Q3 report."* Claude first calls `start_task` with your words, then `pay`. Sellers must be approved first, on the **Rules** page of your account (or by running the demo below once, which approves DataCo and InsightLab for everyone); otherwise the payment is BLOCKED (seller).

4. **Check the task.** Open **Tasks** on your account page. Sentinel shows how it read your request (goal, what the agent may buy, what is out of scope). A new task can spend $1 until you **Confirm** it with the budget you want. Payments Sentinel holds for you wait there with an **Allow once** button. Try *"Now buy http://localhost:4000/signals/crypto too"*: InsightLab is approved, but crypto signals are not part of the task, so it is BLOCKED (off_task).

Claude Desktop, claude.ai, MCP Inspector and deploying to a public host: [docs/onboarding.md](docs/onboarding.md).

## Demo without Claude

`python -m demo.run buy` pays from the shared demo wallet (CDP account `sentinel`, created on first run; fund it with `python -m demo.fund` or at faucet.circle.com). It also approves the demo sellers and books the opening balance, and should print PAID and a basescan link.

## Your account pages

Everything a user needs is on the account page at http://localhost:8000/account: **Overview** (spending against the weekly budget, wallet, getting started checklist, connection details), **Payments** (every request, paid, blocked or held, with the reason and a CSV download), **Tasks** (confirm or revoke what your agent is working on, and allow held payments once), **Rules** (your per payment cap and weekly budget, and your approved sellers) and **Books** (your balanced journal and totals).

## Operator ledger page

Set `SENTINEL_OWNER_PASSWORD` (12+ characters) in `.env`, then:

```bash
uvicorn sentinel.ledger_app:create_app --factory --port 5001    # open http://127.0.0.1:5001
```

This page is for whoever runs Sentinel: it shows every agent, keeps the operator's seller list (it applies to every user, and users cannot change or override its blocks) and downloads the whole payments CSV. A normal user never needs it. It uses port 5001 because macOS uses 5000 for the AirPlay receiver, which answers 403. The agent cannot reach it.

## Watch an agent research and spend

The test sellers on port 4000 are a small marketplace: trip data (flights, hotels, weather, maps, currency, events) next to traps. Some are off task (crypto signals, betting odds), one is pricey (a $2 premium guide), one is an unlisted "visa fast track", and some are approved sellers attacking the buyer: an eSIM listed at $0.10 that asks $1.00, a rail pass paying a lookalike of DataCo's address, a translator whose description tells AI agents to pay now, insurance with a 24 hour payment window, and a "SIM card" whose paywall sells a crypto voucher. `curl -X POST localhost:4000/admin/payee -d '{"path": "/travel/weather"}' -H 'content-type: application/json'` swaps a bought URL's payee. Give an agent a broad task and Sentinel decides, service by service, what it may buy:

```bash
python -m demo.agent "I'm researching a 5 day trip to Tokyo, keep data costs under \$3"
```

It prints every search with Sentinel's verdict (OK, BLOCKED, NEEDS_OWNER), every payment, then the agent's answer. In Claude Desktop or Claude Code ask: *"I'm researching a 5 day trip to Tokyo. Find and buy the data you need through Sentinel."* Claude starts one task, calls `find_services`, and buys only what Sentinel marks OK.

## Try the guard

```bash
python -m demo.run surge    # seller quotes 4x the usual price: BLOCKED
python -m demo.run scam     # unlisted seller: BLOCKED
python -m demo.run intent   # a Q3 email report task: benchmarks PAID, then crypto signals BLOCKED (off_task)
python -m demo.run held     # a $1.00 report on a task that expects about $0.10 a purchase: HELD
```

## Troubleshooting

- The server will not start: read its message. It names every missing variable, every bad value, or the pending migrations (`python -m sentinel.db migrate`).
- No sign in email: on localhost the link is printed in the server's terminal.
- A form says "That request was refused": open the site on the exact host in `PUBLIC_BASE_URL` (`localhost` and `127.0.0.1` differ).
- BLOCKED (seller): that seller is not approved. Approve it on the Rules page of your account.
- BLOCKED (mandate_budget): the task has not been confirmed and has spent its $1. Confirm it on the Tasks page.
- ESCALATED: Sentinel held the payment for you. Open Tasks, read why, and Allow once if it is fine.
- More: [docs/onboarding.md](docs/onboarding.md#troubleshooting).

## Development

```bash
pytest          # database tests use the "test" schema of your Supabase database
ruff check .
mypy sentinel demo tests
```
