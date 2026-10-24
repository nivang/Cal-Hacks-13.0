# Sentinel

Agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel holds the wallet so the agent never does. Every payment request passes through it: guard before payment, books after. Payments are real USDC transfers on Base Sepolia testnet through x402 and the CDP facilitator. Nothing is simulated, and no real money is involved.

## How it fits together

| File | Job |
|---|---|
| `payments.py` | The x402 flow: ask for a price, build a quote, run the guard, sign, pay, record |
| `guard.py` | Pure policy: ALLOW, BLOCK or NEEDS_APPROVAL for one quote |
| `policy.json` | Guard thresholds (price jump multiplier and window, duplicate window, budget window) |
| `attempts.py` | Attempt log and the history lookups the guard reads |
| `ledger.py` | Balanced, append only journal and trial balance |
| `db.py` | Postgres connection pool, migration runner, seed data. The only file that touches the driver |
| `migrations/` | The Postgres schema: tables, append only triggers, balance check, RLS, dashboard read policies |
| `demo.py` | Runs the three guard moments |
| `sellers.py` | Local test sellers (a fixture, not part of Sentinel) |
| `fund.py` | Asks the CDP faucet for testnet USDC |

## Setup

Needs Python 3.13.

    python3.13 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    cp .env.example .env        # then fill in your CDP keys, the three seller addresses and DATABASE_URL

You need a CDP account, an API key and a wallet secret from https://portal.cdp.coinbase.com, and three CDP EVM accounts (seller-a, seller-b, seller-c) whose addresses go in `.env`. The database is Supabase Postgres: create a project, put its pooled connection string in `DATABASE_URL`, then run `python db.py migrate` to build the tables (or apply `migrations/*.sql` in the Supabase SQL editor). The buyer account `sentinel` is created on first use; fund it with `python fund.py` or https://faucet.circle.com (Base Sepolia USDC).

## Run

    uvicorn sellers:app --port 4000      # terminal 1: test sellers
    python demo.py buy                   # terminal 2: clean $0.50 purchase, ALLOW, settles onchain
    python demo.py surge                 # DataCo quotes 4x the usual price: BLOCK
    python demo.py scam                  # unlisted seller, as an injected page would push: NEEDS_APPROVAL

Each run prints the outcome and the trial balance. Blocked and paused attempts still write a row.

## Tests and checks

    pytest -v                            # database tests run in the "test" schema of your Supabase database
    ruff check .
    mypy *.py

`test_guard.py` is the answer key for the guard: six planted cases (unknown seller, cap, budget, price jump, duplicate, clean) plus edge cases. `test_store.py` and `test_payments.py` cover the attempt log, the in flight lock and the books.

## Guard

`guard.evaluate(agent, quote, history)` returns `{decision, rule, detail}`. Rules run in order and the first to fire wins: seller (unlisted or pending needs approval, blocked is blocked), per payment cap, rolling weekly budget, price jump (more than 2x the median of the seller's last 5 settled), duplicate (same agent, payTo, url and amount within 10 min), then ALLOW.

- The first purchase from a seller has no history, so its price is trusted and becomes the baseline.
- A unique index on live attempts means two simultaneous identical calls cannot both pass.
- Payment details come from the seller's quote and are authorized only by the approved seller list, never by text the agent read online. A hidden instruction on a web page cannot add a seller.

## Books

State lives in Supabase Postgres. The code works in integer USDC atomic units (6 decimals) and never floats; the database stores them as `numeric(18,6)` whole USDC, converted in `db.py`, `attempts.py` and `ledger.py`.

- Every payment attempt is recorded, including blocked ones, with the quote, decision, rule fired and status.
- The approved attempt is committed first (it holds the in flight lock while the payment is in the air). When the payment settles, the status change and its journal entry are written in one DB transaction: Dr the agent's expense account, Cr 1010 USDC Cash. Unbalanced entries are rejected before anything is written.
- `response_hash` is the SHA256 of the response body, so you can prove what was received without storing it.

### Append only

The journal and approvals tables cannot be edited, deleted or truncated. Postgres triggers reject it for every role. Every journal entry must also balance (debits equal credits, above zero) or the commit is rejected by a deferred constraint trigger. A correction is a new reversing entry (`ledger.reverse_entry`) that swaps debits and credits and points at the original. History stays intact, which is what makes the books auditable.

### Chart of accounts

| Code | Name | Type |
|------|------|------|
| 1010 | USDC Cash | asset |
| 3000 | Owner Contributions | equity |
| 6100 | Research Data Expense | expense |
| 6200 | Software and Tools Expense | expense |

The wallet's opening USDC balance is booked once as Dr 1010 / Cr 3000 so the trial balance is complete.

## Database notes

- **Clock:** time windows (7 day budget, 10 minute duplicate check) and every timestamp use the database clock. Inside one transaction Postgres `now()` is frozen at the transaction start, so rows written in the same transaction share a timestamp; ties are broken by id.
- **Units:** money is stored as `numeric(18,6)` whole USDC (0.500000). The code works in integer atomic units (500000) and converts at the edges, so the guard and ledger logic never see decimals.
- **Integrity:** append only and balanced entries are enforced by the database for every connection.

## Dashboard access

Row level security is on for every table. The anon role gets nothing. A signed in user can read the data (select only) only if their `app_metadata.role` is `dashboard_viewer`, which can be set server side only (`migrations/005_dashboard_read_policies.sql`). `agents.token_hash` is never readable from the browser.

To let someone view the data, set the claim on their auth user:

    update auth.users set raw_app_meta_data = raw_app_meta_data || '{"role": "dashboard_viewer"}' where email = '...';

## Not built yet

MCP server tools, the human approval step (NEEDS_APPROVAL currently stops the payment and leaves a pending row), month end flags and the QuickBooks export.
