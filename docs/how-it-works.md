# How Sentinel works

Sentinel holds the wallet so the agent never does. Every payment request passes through it: guard before payment, books after. Payments are real USDC transfers on Base Sepolia testnet through x402 and the CDP facilitator. Nothing is simulated, and no real money is involved.

## Layout

| Path | Job |
|---|---|
| `sentinel/mcp_server.py` | The MCP server an agent talks to: tools `pay` and `payment_status`, agent identity, wallet |
| `sentinel/ledger_app.py` | The owner's ledger page: books, seller list, agent limits, payments CSV. Password protected |
| `sentinel/owner.py` | What only the owner can do (approve or block sellers, set limits) and the ledger queries |
| `sentinel/results.py` | The one result shape the tools return, and the plain English for each outcome |
| `sentinel/auth.py` | Agent bearer tokens: issue, rotate, verify (stored only as a hash) |
| `sentinel/urls.py` | Refuses unsafe URLs (file://, private or metadata addresses) before anything is fetched |
| `sentinel/payments.py` | The x402 flow: ask for a price, build a quote, run the guard, sign, pay, record |
| `sentinel/guard.py` | Pure policy: ALLOW or BLOCK for one quote |
| `sentinel/policy.json` | Guard thresholds (price jump multiplier and window, duplicate window, budget window) |
| `sentinel/attempts.py` | Attempt log and the history lookups the guard reads |
| `sentinel/ledger.py` | Balanced, append only journal and trial balance |
| `sentinel/db.py` | Postgres connection pool, migration runner, seed data. The only file that touches the driver |
| `sentinel/migrations/` | The Postgres schema: tables, append only triggers, balance check, RLS, dashboard read policies |
| `sentinel/templates/` | Ledger page HTML |
| `demo/run.py` | Runs the three guard moments (buy, surge, scam) |
| `demo/sellers.py` | Local test sellers (a fixture, not part of Sentinel) |
| `demo/fund.py` | Asks the CDP faucet for testnet USDC |
| `tests/` | pytest suite |

## MCP tools

- `pay(url, reason)`: buy a paywalled URL. `reason` must be specific (20 to 500 characters); the owner reads it in the ledger next to the seller and the price. Returns one of `SETTLED` (body, amount, tx hash), `BLOCKED` (the rule and a plain English reason), `FREE`, or `FAILED`.
- `payment_status(request_id)`: the current state of one of the agent's own requests, including the paid body once it has settled.

There is no tool to add sellers or change limits on purpose: only the owner does that, on the ledger page. A seller that is not on the list is blocked, not queued for approval.

Over stdio the agent's identity is `SENTINEL_AGENT_TOKEN` (checked against the database at startup). Over HTTP (`python -m sentinel.mcp_server --transport http`, Streamable HTTP at http://127.0.0.1:8000/mcp) every request must carry `Authorization: Bearer <token>`; a missing or wrong token gets a 401. `SENTINEL_ALLOW_LOCAL_URLS=1` lets the localhost test sellers be reached; leave it unset on a hosted server.

Try it with the MCP Inspector (start the test sellers first):

    npx @modelcontextprotocol/inspector .venv/bin/python -m sentinel.mcp_server

    npx @modelcontextprotocol/inspector --cli .venv/bin/python -m sentinel.mcp_server --method tools/call --tool-name pay \
      --tool-arg url=http://localhost:4000/benchmarks/email \
      --tool-arg "reason=Need 2026 email open-rate benchmarks for the Q3 report; DataCo is named in the brief."

The payment path makes blocking database calls inside async code, which is fine for a demo but adds a few tens of milliseconds per call. Everything an agent sends (`reason`) and receives (`body`) is untrusted text; escape it in any dashboard.

## Ledger page

It shows each agent's spend against its weekly budget (with a form to change the cap and budget), the approved sellers (approve a new address, block or re approve one), every payment attempt (agent, reason, seller, price, decision, rule, status, tx link), the journal and the trial balance. **Download payments CSV** exports one row per attempt for the accountant.

It runs as its own process, separate from the MCP server, and the agent never holds the password, so the agent can ask to pay but cannot change who it may pay. Reasons and URLs come from the agent and are escaped on the page; CSV cells that a spreadsheet would run as a formula get a leading apostrophe.

## Guard

`guard.evaluate(agent, quote, history)` returns `{decision, rule, detail}`. Rules run in order and the first to fire wins: seller (anything the owner has not approved is blocked), per payment cap, rolling weekly budget, price jump (more than 2x the median of the seller's last 5 settled), duplicate (same agent, payTo, url and amount within 10 min), then ALLOW.

- The first purchase from a seller has no history, so its price is trusted and becomes the baseline.
- A unique index on live attempts means two simultaneous identical calls cannot both pass.
- Payment details come from the seller's quote and are authorized only by the approved seller list, never by text the agent read online. A hidden instruction on a web page cannot add a seller.

## Books

State lives in Supabase Postgres. The code works in integer USDC atomic units (6 decimals) and never floats; the database stores them as `numeric(18,6)` whole USDC, converted in `db.py`, `attempts.py` and `ledger.py`.

- Every payment attempt is recorded, including blocked ones, with the quote, decision, rule fired and status.
- The approved attempt is committed first (it holds the in flight lock while the payment is in the air). When the payment settles, the status change and its journal entry are written in one DB transaction: Dr the agent's expense account, Cr 1010 USDC Cash. Unbalanced entries are rejected before anything is written.
- `response_hash` is the SHA256 of the response body, so you can prove what was received without storing it.

### Append only

The journal, approvals and stored results tables cannot be edited, deleted or truncated. Postgres triggers reject it for every role. Every journal entry must also balance (debits equal credits, above zero) or the commit is rejected by a deferred constraint trigger. A correction is a new reversing entry (`ledger.reverse_entry`) that swaps debits and credits and points at the original. History stays intact, which is what makes the books auditable.

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

Row level security is on for every table. The anon role gets nothing. A signed in user can read the data (select only) only if their `app_metadata.role` is `dashboard_viewer`, which can be set server side only (`sentinel/migrations/005_dashboard_read_policies.sql`). `agents.token_hash` is never readable from the browser.

    update auth.users set raw_app_meta_data = raw_app_meta_data || '{"role": "dashboard_viewer"}' where email = '...';

## Tests

Database tests run in the `test` schema of your Supabase database, each inside a rolled back transaction.

`test_guard.py` is the answer key for the guard: six planted cases (unknown seller, cap, budget, price jump, duplicate, clean) plus edge cases. `test_store.py` and `test_payments.py` cover the attempt log, the in flight lock and the books. `test_pay_flow.py` runs the whole payment flow against a fake seller and covers every way an approved payment can fail. `test_mcp_server.py` calls the tools through an in memory MCP client and checks each result the agent can see, agent identity and the HTTP 401s. `test_owner.py` covers the seller list and limits the guard obeys, the ledger queries and the CSV. `test_ledger_app.py` covers the password gate, the forms, escaping and the CSV download.

## Not built (by design)

No per payment human approval. The owner approves sellers, not payments: an unlisted seller is blocked outright, and the owner adds it on the ledger page if it is legitimate. The `approvals` table and the `pending` / `denied` attempt statuses remain in the schema unused (migration 007 closed out the old pending rows).

Not built yet: month end flags and the QuickBooks export.
