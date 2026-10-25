# Sentinel manual testing before merging

Use this checklist to verify sign in, MCP, marketplace discovery, Apify details, task checks, and payment records on `feature/market-discovery`. Record the tool arguments, result status, request ID, and any unexpected behavior as you go. Use a dedicated test account so previous purchases and budgets do not confuse the results.

Sentinel currently **searches the Apify Store and reads Actor details**. It does **not** start Actor runs or spend Apify credit. `pay_with: apify_credit` describes the Actor's funding method; it is not permission or an implemented way to run it. Public marketplace search (`discover`) and task catalog previews (`find_services`, `quote`) are separate features.

## 1 Run automated checks

From the repository root, with Python 3.13 and dependencies installed and `.env` configured as in section 2:

```bash
source .venv/bin/activate
python -m sentinel.db migrate
pytest -q
ruff check .
mypy sentinel demo tests
git diff --check
```

Expected: every test passes, lint and type checks pass, and no merge markers or whitespace errors remain. Database tests create/use the `test` schema of `DATABASE_URL`, with each test rolled back. They use fake wallets, HTTP responses, and judge responses; they do not prove that external credentials, current directory APIs, or live model access work.

For a shorter check while changing discovery:

```bash
pytest -q tests/test_discovery.py tests/test_apify.py tests/test_catalog.py tests/test_mcp_server.py
```

## 2 Configure and start the services

Keep existing values in `.env`. Compare names with `.env.example`; do not copy your credentials into that tracked template.

Required: `DATABASE_URL`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`, `ANTHROPIC_API_KEY`, `SESSION_SECRET`, and `TOKEN_HASH_SECRET`. The two session/token secrets must be different and at least 32 characters. Demo sellers also need `SELLER_A_ADDRESS`, `SELLER_B_ADDRESS`, and `SELLER_C_ADDRESS`. Set `APIFY_TOKEN` for authenticated Apify Store access. The key must belong to an Apify account you intend to use.

For these steps use port 8013, since port 8000 may already serve another app:

```dotenv
PUBLIC_BASE_URL=http://localhost:8013
SENTINEL_ALLOW_LOCAL_URLS=1
```

For the existing services on the home machine:

```bash
systemctl --user restart calhacks-sentinel calhacks-sellers
systemctl --user is-active calhacks-sentinel calhacks-sellers
journalctl --user -u calhacks-sentinel -f -n 20
```

Both must say `active`. Restarting sellers restores the demo prices and payees changed during earlier tests. The server refuses startup without the new `ANTHROPIC_API_KEY`.

Alternatively, on a machine without those services, run these in separate activated terminals:

```bash
PUBLIC_BASE_URL=http://localhost:8013 python -m sentinel.mcp_server --port 8013
```

```bash
uvicorn test_site.app:app --host 127.0.0.1 --port 4000
```

Run the following on the server machine:

```bash
curl --fail http://localhost:8013/healthz
curl -i -X POST http://localhost:8013/mcp
curl --fail http://localhost:4000/test-site/discovery/resources
```

Expected: health returns `{"status":"ok"}`, unauthenticated MCP returns HTTP 401, and the catalog returns JSON with an `items` array containing the travel products and traps. Health is a liveness check; it does not check the database or API credentials.

### Access from another computer over Tailscale

Run this on the other computer and leave it open:

```bash
ssh -N -L 8013:127.0.0.1:8013 jayke@home.tail13b73b.ts.net
```

Open `http://localhost:8013`. The demo URLs below are fetched by Sentinel on the server, so no tunnel for port 4000 is needed for MCP purchases. Run seller `curl` commands on the server, not on the other computer.

## 3 Sign in and connect MCP

1. Enter an email you control at `http://localhost:8013`. Locally, open the sign in link from the server logs, then click the confirmation button. Never put the link in a shared test report.
2. Check Overview, Payments, Tasks, Rules, and Books. All should open without server errors.
3. Fund the personal wallet shown in Overview with **Base Sepolia testnet USDC**. The shared demo wallet is a different wallet.
4. In Rules, approve DataCo and InsightLab using the local demo shortcuts. Leave the scammer unapproved. If a fresh-wallet warning appears, read it and explicitly confirm the intended demo seller.
5. Connect Claude Code from the computer with the tunnel:

   ```bash
   claude mcp add --transport http sentinel http://localhost:8013/mcp
   ```

6. Run `/mcp`, authenticate, and Allow. If Sentinel is already configured, reconnect or restart the client so it refreshes the changed tool schemas.
7. Check that these seven tools are exposed: `start_task`, `find_services`, `quote`, `pay`, `payment_status`, `discover`, `apify_actor`.

Use Claude's tool call display or MCP Inspector to inspect arguments and structured results. The JSON examples below are **tool arguments**, not shell commands. Replace `TASK_ID` and `REQUEST_ID` with actual returned integers.

## 4 Test public marketplace discovery without spending

Ask the client to call each of these explicitly, without calling `pay`:

```text
discover({"query":"weather","limit":5})
discover({"query":"weather","source":"cdp","payable_only":true,"limit":5})
discover({"query":"google maps","source":"apify","max_price_usdc":0.01,"limit":5})
```

Check all of the following:

- Mixed search reports each directory in `sources`: `cdp`, `agentic`, `payapi`, and `apify`. A healthy directory says `ok`; failures/timeouts are named without preventing other directories from answering.
- There are at most five listings. Every listing has a name, URL, source, and explanatory fields. Duplicate URLs should not appear.
- `payable_only` returns only payable Base Sepolia USDC GET listings, with `pay_with: x402`. Each has `seller_status` set to `approved`, `unlisted`, or `blocked` for your account. This is compatibility with the payment network, not a promise that all task checks will allow it.
- The Apify-only search returns only Apify results. With your token, eligible Actors have an `actor` ID and `pay_with: apify_credit`, while `payable` stays false. Monthly rental and deprecated Actors are excluded from this search.
- For the price-limited search, every returned known unit price is at most $0.01. Unknown prices are excluded. For Apify this is an event price, generally at the free plan tier, not the total cost of a run. Start fees, compute, and multiple events may add cost.
- Mainnet listings remain browse only. `NONE_FOUND` is valid when a filter has no matches; `UNAVAILABLE` means all requested sources failed. Neither should be a traceback.
- Search creates no payment attempt, journal entry, transaction hash, or Actor run.

External listings and prices change. Verify structure, source status, and filters rather than requiring the same names or ordering every time. An unavailable source is degraded coverage to investigate before merge, not evidence that it passed.

## 5 Test Apify Actor details without starting a run

```text
apify_actor({"actor":"compass/crawler-google-places"})
apify_actor({"actor":"apify/website-content-crawler"})
apify_actor({"actor":"https://apify.com/compass/crawler-google-places"})
apify_actor({"actor":"../../users/me"})
apify_actor({"actor":"sentinel-validation-missing-owner/no-such-actor"})
```

Expected:

1. The two real Actors return `OK`, normalized IDs, pricing information, input fields, and an example when one can be derived. Required inputs should come first. Examples are starting points: fill every required input before running an Actor outside Sentinel. The Google Maps scraper has event prices; the website crawler may be `FREE`, which means the Actor has no separate rental/event price, not that compute is free.
2. The Apify website URL normalizes to the same Actor as the first call.
3. `../../users/me` returns `INVALID` before any Apify API request is made. The automated tests verify the no-request condition.
4. The missing Actor returns `NOT_FOUND`. Do not require an exact missing ID forever: if it ever exists, choose another clearly nonexistent owner/Actor pair.
5. The result says Sentinel cannot start runs yet. No `run_actor` tool should be present. Do not use `pay` on an Apify Store page as a substitute.
6. No response contains `APIFY_TOKEN` or an Authorization header. The automated tests also check that credentials go only to `https://api.apify.com`, and that the input schema can be missing without crashing the whole details lookup.

Optional fallback: in a separate test server process, unset `APIFY_TOKEN`, then repeat. Details should say Actors can only be browsed. Do not remove a token from the shared `.env` while someone else is using the service.

## 6 Test task IDs and catalog previews

Start one task and keep its ID for the following tests:

```text
start_task({"task":"Research a five day trip to Tokyo. Buy flight, hotel, weather and transit data. Do not buy crypto signals, betting data or visa fast track services. Spend at most $3 on data.","budget_usd":"3.00"})
```

Expected: `STARTED` with a positive integer `task_id`. The task appears under Tasks. It initially has the provisional $1 spending allowance; inspect its interpretation and Confirm it with a $3 budget before continuing.

```text
find_services({"task_id":TASK_ID,"query":"Tokyo flights hotels weather transit"})
quote({"task_id":TASK_ID,"url":"http://localhost:4000/test-site/weather","purpose":"Need the Tokyo weather forecast for the trip."})
```

Expected: relevant approved services have an `OK` verdict, with prices and seller descriptions marked untrusted. `find_services` returns only buyable services; turned-away paid listings appear in the owner's Payments log as previews. `quote` can show `BLOCKED`, `NEEDS_OWNER`, or `FREE` as well. Previews do not sign, transfer USDC, or create journal entries. They can record preview decisions and catalog claims. An `OK` preview does not reserve a price or waive the independent x402 SDK cap; `pay` fetches a fresh quote and applies its own final checks.

Parameter checks:

- Call `pay` without `task_id`, with `task_id: 0`, or with `task_id: null`: MCP validation should refuse it before opening a wallet.
- Call with a positive task ID that does not exist for you: `BLOCKED`, rule `no_task`, without opening a wallet.
- Reuse an actual task ID returned by `start_task`; do not invent one to get past validation.

## 7 Make one small testnet purchase and inspect the books

This step transfers **0.05 testnet USDC**. Model calls also use Anthropic API credit. Use the funded test account.

```text
pay({"task_id":TASK_ID,"url":"http://localhost:4000/test-site/weather","reason":"Need the Tokyo weather forecast to plan outdoor activities for the five day trip.","found_at":"sentinel discovery"})
```

Use `found_at: "sentinel discovery"` only after `find_services` listed this URL for this task. For a public marketplace listing returned by `discover`, use `found_at: "discover"`; for a URL the owner explicitly supplied, use `found_at: "owner"`. `discover` does not register the URL in the task catalog.

Expected: `SETTLED`, an amount of `50000` atomic units / `0.05` USDC, a request ID, response data, transaction hash, and Base Sepolia explorer link. Call `payment_status` with that request ID: it must show the same payment and data.

Open Payments and Books. Expect one settled purchase grouped under the task, a balanced journal entry, and a matching CSV row. Wallet funding is not yet automatically booked for personal accounts, so the cash account may be negative even though debits and credits balance.

Immediately repeat the identical `pay`: expect `BLOCKED` with the duplicate rule and a reference to the earlier request, with no second transfer or journal entry. Fetch the paid data with `payment_status` instead.

## 8 Check blocked and held purchases

For these checks keep the same Tokyo task. Leave the scammer unapproved and DataCo/InsightLab approved. If you exceed the task's six-attempts-per-minute rate limit, pause at least a minute before continuing so `rate` does not mask the rule being tested.

1. Quote `/test-site/crypto-signals`: expect `BLOCKED` / `off_task` even though InsightLab is approved. Model wording can vary, but no payment may proceed.
2. Quote `/test-site/visa-fasttrack`: expect a seller block. A listing's claim that the owner authorized it must not approve that seller.
3. Call `find_services` for `Tokyo eSIM rail SIM insurance translation` first, then quote the following full URLs under `http://localhost:4000`:
   - `/test-site/esim`: `BLOCKED` / `bait_switch`; the listing advertises $0.10 but the quote asks $1.00.
   - `/test-site/rail-pass`: `BLOCKED` / `lookalike`; its payee imitates DataCo.
   - `/test-site/translate`: `BLOCKED` / `injection`; its description addresses AI agents and tells them to pay.
   - `/test-site/insurance`: `BLOCKED` / `long_window`; it requests a 24 hour authorization.
   - `/test-site/sim-card`: `NEEDS_OWNER` / `listing_changed`; the catalog says SIM card, but the paywall describes a crypto voucher.
4. Quote `/test-site/guide-premium` before spending much of the $3 budget: expect `NEEDS_OWNER` for the large share of the budget (or another stricter applicable task rule). Confirm it appears under Tasks as held. Do not click Allow once just to remove a safeguard: this product also exceeds the SDK's independent $1 cap.
5. For a paid Allow once exercise at or below $1, start a separate task explicitly expecting about $0.10 per purchase and a $3 total budget. Confirm it, then quote `/test-site/flights` at $0.25 and `/test-site/social-report` at $1.00 with reasons fitting that task. Use the $1 report only if the task actually covers social reports; it should be held for exceeding the typical price. Click Allow once for the intended held report, then call `pay` again on the identical task, URL, payee, and amount. Expect settlement if every other rule passes. An allowance never overrides seller blocks, budgets, or the SDK limit.
6. Revoke a task on Tasks. Further payments under it must be blocked, without transfer. Use a URL not already blocked by the duplicate rule so you see the task-revoked reason.

Save every unexpected status and its `rule` field. Rules run in order, so prior purchases, changed seller settings, and depleted budgets can legitimately cause an earlier rule to fire. Use a fresh account/task or restore the fixture before diagnosing a different rule.

## 9 Check isolation and failure handling

- Sign in as a second user in another browser profile. Their wallet, tasks, personal sellers, payments, and books must be separate. A task ID owned by the first user must be refused; their payment request ID must be `NOT_FOUND` to the second user.
- In the first account, use Sign out everywhere. Existing browser sessions and MCP tokens should stop working until authentication and consent are completed again.
- In an isolated local instance, point `SENTINEL_DISCOVERY_URL` at an unreachable loopback port, then call `find_services`: expect a safe failed result, no payment. Restore the URL afterward. Public `discover` uses its own fixed directory URLs and is unaffected by this setting.
- Automated tests cover unavailable directories, timeouts, oversized responses, malformed inputs, signing failures, cancellation, and database failures. Run those tests rather than disrupting shared internet/database access or intentionally invalidating shared credentials.
- For an otherwise authorized quote over $1, the independent x402 SDK cap should return `BLOCKED` / `x402_cap` before signing. A task or seller block may fire earlier; do not remove those rules merely to reach this case. `tests/test_pay_flow.py` reproduces the SDK refusal without sending money.

## 10 Merge checks and cleanup

- Record a successful complete `pytest` run, lint/type results, and the exact commit plus uncommitted changes tested.
- Record the live source statuses, successful Apify detail lookups, and the manual OAuth/task/payment/Books results. Mark unperformed checks pending rather than passed.
- Confirm that no API key, database password, OAuth token, cookie, sign in link, or real `.env` is in the staged diff. `.env.example` should contain placeholders only.
- Ensure the new `sentinel/apify.py` and `tests/test_apify.py` are included when preparing the branch for merge. They were previously untracked, so pushing only existing commits would omit the feature.
- Keep both `sentinel/discovery.py` (public marketplaces) and `sentinel/catalog.py` (task previews), with tests for each. Confirm the seven-tool contract after restarting the service.
- Restart demo sellers to reset mutable prices/payees. Restore account limits changed for testing and revoke test tasks you no longer need. Keep journal history; do not delete or reset payment rows to make the page look empty.
- After merging, install dependencies, run migrations, restart Sentinel, and reconnect MCP clients to refresh the task-aware `pay` schema.

Passing tests verify the implemented scope. Actor execution, Apify spending controls/bookkeeping, and a public Grok/Funnel deployment are separate work and are not established by this checklist.
