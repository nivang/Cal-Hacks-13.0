# How Sentinel works

Sentinel holds the wallet so the agent never does. Every payment request passes through it: guard before payment, books after. Payments are real USDC transfers on Base Sepolia testnet through x402 and the CDP facilitator. Nothing is simulated, and no real money is involved.

## Layout

| Path | Job |
|---|---|
| `sentinel/mcp_server.py` | The MCP tools `pay` and `payment_status`: identity from the OAuth token, the user's own wallet per call, in flight limits. `main()` checks env, settings and migrations, then serves the whole app |
| `sentinel/ledger_app.py` | The operator's ledger page (port 5001): every agent's books, the operator's seller list, limits, payments CSV. Password protected |
| `sentinel/owner.py` | The operator's actions (the operator seller list, set limits, `parse_usd`) and the ledger queries, also used per agent by the account pages |
| `sentinel/results.py` | The one result shape the tools return, and the plain English for each outcome |
| `sentinel/urls.py` | Refuses unsafe URLs (file://, private or metadata addresses) before anything is fetched |
| `sentinel/payments.py` | The x402 flow: ask for a price, build a quote, run the guard, sign, pay, record |
| `sentinel/wallets.py` | The wallet that signs: `open_user_wallet(user_id)`, each user's own CDP account named by their uuid, behind x402's signer interface |
| `sentinel/guard.py` | Pure policy: ALLOW or BLOCK for one quote |
| `sentinel/policy.json` | Guard thresholds (price jump multiplier and window, duplicate window, budget window) |
| `sentinel/attempts.py` | Attempt log and the history lookups the guard reads (price history per agent) |
| `sentinel/ledger.py` | Balanced, append only journal and trial balance; credits the agent's own cash account |
| `sentinel/db.py` | Postgres connection pool (max 10), tracked migrations, seed data and default limits. The only file that touches the driver |
| `sentinel/migrations/` | The Postgres schema: tables, append only triggers, balance check, RLS, dashboard read policies; 008 adds users, magic links and the OAuth tables; 009 adds `sellers.agent_id` (personal seller lists) |
| `sentinel/web/__init__.py` | `Settings` and `load_settings`: reads and checks the environment once, derives one subkey per purpose |
| `sentinel/web/app.py` | The one ASGI app: site routes, consent, security headers, CORS on `/mcp`, then FastMCP mounted at `/` |
| `sentinel/web/auth.py` | Magic links, email normalization, session cookies, CSRF, same origin check |
| `sentinel/web/oauth.py` | The OAuth server: FastMCP's `OAuthProvider` over Postgres, registration rules, consent requests, codes, tokens |
| `sentinel/web/books.py` | What a signed in user sees and changes for their own agent: merged seller list, payments, journal, totals, summary, live grant check |
| `sentinel/web/users.py` | Users, their wallet and books on first sign in, the `Identity` a token carries, sign out everywhere |
| `sentinel/web/ratelimit.py` | Limits for sign in links and client registration, counted from the rows those write |
| `sentinel/web/mailer.py` | The sign in email: Resend in production, stderr on a local run |
| `sentinel/web/pages.py`, `templates/`, `static/` | The site's pages (Jinja2, autoescaped, no JavaScript of ours) |
| `sentinel/templates/` | Ledger page HTML |
| `demo/__init__.py` | `DEMO_ACCOUNT = "sentinel"`: the only home of the shared test wallet |
| `demo/run.py` | Runs the three guard moments (buy, surge, scam) as research-agent from the demo wallet |
| `demo/sellers.py` | Local test sellers (a fixture, not part of Sentinel) |
| `demo/fund.py` | Asks the CDP faucet for testnet USDC |
| `docs/onboarding.md` | Sign in, connecting Claude, local run, deploy checklist, troubleshooting |
| `tests/` | pytest suite |

## MCP tools

- `pay(url, reason)`: buy a paywalled URL. `reason` must be specific (20 to 500 characters); the owner reads it in the ledger next to the seller and the price. Returns one of `SETTLED` (body, amount, tx hash), `BLOCKED` (the rule and a plain English reason), `FREE`, or `FAILED`.
- `payment_status(request_id)`: the current state of one of the agent's own requests, including the paid body once it has settled.

There is no tool to add sellers or change limits on purpose: only the signed in user does that, on the Rules page of their account (or the operator, on the ledger page). A seller that is not on the list is blocked, not queued for approval.

The tools are served over Streamable HTTP only, at `PUBLIC_BASE_URL/mcp` (`python -m sentinel.mcp_server`, default http://127.0.0.1:8000/mcp), stateless and JSON, so every request is authenticated again. Every request carries an OAuth access token; a missing, expired, revoked or foreign one gets a 401 (see [Onboarding and identity](#onboarding-and-identity)). There is no stdio transport and no agent token any more. `SENTINEL_ALLOW_LOCAL_URLS=1` lets the localhost test sellers be reached; the server refuses it outside localhost.

In `pay`, in order: the URL check (BLOCKED url), an in flight slot (2 per user, 6 per process; over it is FAILED with nothing paid), opening the user's wallet by user id (lookup only; a failure is FAILED with nothing paid and no attempt row), a check that the wallet's address is the one on file (else FAILED, nothing signed), then the payment. `payment_status` reads only the caller's own agent's requests.

To try it by hand, connect the MCP Inspector or Claude Code as in [onboarding.md](onboarding.md).

The payment path makes blocking database calls inside async code, and the CDP signer makes one blocking call per signature inside the event loop, which is fine for a demo but adds a few tens of milliseconds per call. Everything an agent sends (`reason`) and receives (`body`) is untrusted text; escape it in any dashboard.

## Onboarding and identity

One process serves three things on one origin, `PUBLIC_BASE_URL` (B): the sign in site, the OAuth server Claude connects through, and `/mcp`. `web/app.py` builds it: FastAPI site routes first, then `/static`, then FastMCP's app mounted at `/`, which serves `/mcp`, `/authorize`, `/token`, `/register`, `/revoke` and the discovery documents through `web/oauth.py`. B, with no path and no trailing slash, is the OAuth issuer, byte identical in every metadata document and every `iss`; the resource every token is bound to is `B/mcp`. Setup for users and operators: [onboarding.md](onboarding.md).

**Startup.** `python -m sentinel.mcp_server [--host] [--port]` names every missing variable in one exit (database, CDP, `PUBLIC_BASE_URL`, `SESSION_SECRET`, `TOKEN_HASH_SECRET`, and the mail settings outside dev mode), then every bad value in another (https outside loopback, secrets of 32+ characters that differ, `SENTINEL_ALLOW_LOCAL_URLS` refused outside dev mode; `FORWARDED_ALLOW_IPS=*`, or unset outside dev mode, only warns), then refuses a database with pending migrations. Dev mode means `PUBLIC_BASE_URL` and the bind host are both loopback; only then may mail settings be empty, and sign in links print to stderr.

**Sign in.** `POST /signin` normalizes the address, checks the rate limits and stores the link's HMAC in one transaction, then mails the link (Resend, with an idempotency key per link). The "check your email" page is the same for known and unknown addresses. `GET /auth/verify` only shows a "Sign in as ..." button; `POST /auth/verify` spends the link atomically (15 minutes, once). A session is a signed cookie (24 hours) carrying the user's `session_epoch` and a random nonce, so no two sign ins share a cookie or CSRF tokens; **sign out everywhere** bumps the epoch and revokes every grant. Every site POST must come from B (Origin, or `Sec-Fetch-Site: same-origin`), and session bound forms also carry an HMAC CSRF token.

**First sign in.** The user row is created by email, then `users.ensure_wallet` gets or creates the CDP account named by the user's uuid (15 s timeout). Only if CDP answered, one transaction with the user row locked creates agent `user-<uuid>`, cash account `1010-<agent id>`, the default limits ($10 per payment, $50 per week) and stores the address. It is idempotent, so a retry or a second tab never makes a second wallet, agent or account. If CDP fails the user stays signed in and the account and consent pages offer a retry; no code is ever issued without a wallet.

**Connecting an app.**
1. `POST /mcp` without a token: 401 with `WWW-Authenticate: Bearer` naming scope `pay` and the protected resource metadata (served at `/.well-known/oauth-protected-resource/mcp` and at the root path). `/mcp` answers CORS, so MCP Inspector can connect straight from the browser.
2. Registration (`POST /register`, JSON only, at most 16 KB). Token endpoint auth is `none` or `client_secret_post`; a request for `client_secret_basic` gets `client_secret_post` instead, since the SDK reads the client id from the form body only. The client secret is derived from the client id and never stored. 1 to 10 redirect URIs of at most 512 characters: https on a named host, http only on exactly `localhost`, `127.0.0.1` or `[::1]`, or a private app scheme. Refused: Sentinel's own host, `localhost` and any `*.localhost` name except as plain `http://localhost`, browser schemes that wrap another URL (`microsoft-edge:https://...`), unsafe schemes, userinfo, fragments, dot segments and non loopback IP literals. Only an allowlist of metadata fields is kept, the name is sanitized and capped at 100 characters, and clients that never complete a consent are purged after 30 days.
3. Registration limits: registrations from Anthropic's egress range (160.79.104.0/21, where every claude.ai user registers from) skip the per IP limit and have their own budget of 500 per hour; all other registrations share a separate 500 per hour plus 20 per hour per IP. IPv6 is grouped by /48 for registrations and by /64 for sign in links.
4. `/authorize` runs FastMCP's checks (client, exact redirect URI except that a loopback http URI may use any port, PKCE S256, scope), then Sentinel's: the resource must be `B/mcp` (absent counts as ours), `state` at most 512 characters, and the redirect never points at Sentinel. It answers with a redirect to `/oauth/consent?req=<signed pending request>` (20 minutes, no table) and sets a cookie binding that request to this browser (named after the request's nonce, so two flows in one browser do not overwrite each other; Allow and Deny clear only their own). Errors are redirected only to Claude's exact callbacks (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) or literal loopback; any other client gets an error page, so `/authorize` is never an open redirector.
5. Consent needs the binding cookie, a session (else a sign in that comes back) and a wallet. The page shows where the code goes (host in punycode, large), a caution box unless it is a Claude callback or loopback, the app supplied name labelled as such, the user's email and wallet, and Allow and Deny. Its CSP `form-action` adds the redirect origin so the browser follows the redirect after Allow. **Allow** checks Origin and a CSRF token bound to the session and the request, rechecks under a row lock that the session epoch is still current (so a sign out everywhere in between wins), and writes the code row: one per consent request, 5 minutes. It redirects with `code`, `state` if sent, and `iss`. **Deny** returns `error=access_denied` the same way.
6. `/token` swaps the code (client auth, PKCE, redirect URI) for a 1 hour access token and a 30 day refresh token, both capped by the grant's 90 days. A replayed code revokes the grant. Refresh rotates the pair; a retry within 30 seconds gets the same new pair (it is derived from the presented token), a reuse after that revokes the grant. Refresh accepts `offline_access` in the requested scope, which is tolerated and never advertised; the only scope granted is `pay`.
7. `/revoke` is Sentinel's own: public clients (`none`) can revoke too, and revoking any token revokes its whole grant.

**Every MCP call.** `load_access_token` joins token, grant, user, agent and wallet and requires a live access token under a grant that is neither revoked nor past 90 days, the resource `B/mcp`, and a user with a wallet and an agent with its own cash account. Anything else is a 401. The token's claims carry `sub` (the user id), `agent` and `wallet`; `pay` acts only for that identity and only with that user's own wallet.

**Storage.** Links, codes, access and refresh tokens are stored only as HMACs under `TOKEN_HASH_SECRET` (one subkey per purpose); IPs only as HMACs too. Cookies, CSRF tokens and pending requests are signed with `SESSION_SECRET`. Logs name user ids and client ids, never emails, links, tokens, cookies or CSRF values, and query strings are stripped from the access log. Site pages get a strict CSP (Tailwind's pinned browser build is the only script), `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `nosniff`, `no-store`, and HSTS on https.

## Account pages

A signed in user does everything from `/account` (all need a live session, otherwise a 303 to `/signin?next=<page>`):

| Route | What |
|---|---|
| `GET /account` | Overview: spend against budget, cap, wallet and Fund it, getting started checklist, connection details, recent payments, sign out everywhere |
| `GET /account/payments`, `GET /account/payments.csv` | Their own payments (newest 200) with status and a plain English why; the CSV holds up to the latest 20,000 |
| `GET /account/rules`, `POST /account/rules/limits`, `POST /account/rules/sellers` | Their per payment cap and weekly budget; their seller list (add, block, re approve; up to 200 personal sellers, then block one you no longer use) |
| `GET /account/books` | Their journal, totals by account and whether the books balance |

Every query is scoped to the agent id read from the database for the session's user, never from a form or URL, and every POST needs the same origin and the account CSRF token. In dev mode the Rules page also offers the demo sellers (DataCo, InsightLab, from `SELLER_A_ADDRESS` and `SELLER_B_ADDRESS`; never the scammer). The journal shows each entry as "Paid <seller> for <host and path>" with a Basescan link, the raw memo kept as a tooltip. `web/books.py` holds the queries.

## Ledger page

This page is for the operator and runs on port 5001 (macOS uses 5000 for the AirPlay receiver, which answers 403). Users do their own sellers, limits and books on the account pages. Here the operator keeps the list that applies to every agent.

It shows each agent's spend against its weekly budget (with a form to change the cap and budget), the approved sellers (approve a new address, block or re approve one), every payment attempt (agent, reason, seller, price, decision, rule, status, tx link), the journal and the trial balance. **Download payments CSV** exports one row per attempt for the accountant.

It runs as its own process, separate from the MCP server, and the agent never holds the password, so the agent can ask to pay but cannot change who it may pay. Reasons and URLs come from the agent and are escaped on the page; CSV cells that a spreadsheet would run as a formula get a leading apostrophe.

## Wallet

`payments.py` never touches a wallet SDK. It takes an x402 `ClientEvmSigner` (an address plus `sign_typed_data`). Production opens it with `wallets.open_user_wallet(user_id)`: one CDP server account per user, named by `users.id` (a uuid), so the key stays inside Coinbase and each signature is a CDP API call. A pay call only looks the account up (`create=False`), so paying can never mint an empty wallet; only the first sign in creates it (`create=True`). There is no default wallet. The shared `sentinel` account is `demo.DEMO_ACCOUNT`, opened only from `demo/` through `wallets.cdp_wallet`. `SENTINEL_WALLET` picks the wallet provider (default `cdp`, the only one built in). A new provider is one opener in `wallets.py` plus its entry in `WALLETS`; the guard, the ledger and the tools do not change. A raw private key wallet is left out on purpose, since the key would sit in env outside CDP's custody. Every pay call opens the user's account for that call only and refuses to sign unless its address equals `users.wallet_address`. Account lookups and creation time out after 15 seconds. CDP's usage and error analytics are turned off by default (`DISABLE_CDP_USAGE_TRACKING`, `DISABLE_CDP_ERROR_REPORTING`), since they stalled calls.

## Guard

`guard.evaluate(agent, quote, history)` returns `{decision, rule, detail}`. Rules run in order and the first to fire wins: seller (anything the owner has not approved is blocked), per payment cap, rolling weekly budget, price jump (more than 2x the median of this agent's last 5 settled payments to the seller), duplicate (same agent, payTo, url and amount within 10 min), then ALLOW.

Every user is their own agent, so the cap, the budget, the duplicate window and the price jump baseline are all per user: one user cannot see or move another's baseline. Sellers come from two lists: the operator's (`sellers.agent_id` null, applies to every agent) and the user's own (`agent_id` = their agent). For one agent the applicable rows are those two sets; any applicable blocked row wins, then any approved row, else the seller is unlisted and blocked (`attempts.load_history`, one query; `guard.py` stays pure). A user can add, block and re approve only their own rows, so one user's approval never opens a seller for anyone else, and an operator block cannot be overridden.

- An agent's first purchase from a seller has no history, so its price is trusted and becomes the baseline.
- A unique index on live attempts means two simultaneous identical calls cannot both pass.
- Payment details come from the seller's quote and are authorized only by the approved seller list, never by text the agent read online. A hidden instruction on a web page cannot add a seller.

## Books

State lives in Supabase Postgres. The code works in integer USDC atomic units (6 decimals) and never floats; the database stores them as `numeric(18,6)` whole USDC, converted in `db.py`, `attempts.py` and `ledger.py`.

- Every payment attempt is recorded, including blocked ones, with the quote, decision, rule fired and status.
- The approved attempt is committed first (it holds the in flight lock while the payment is in the air). When the payment settles, the status change and its journal entry are written in one DB transaction: Dr the agent's expense account, Cr the agent's cash account. Each signed in user's agent has its own, `1010-<agent id>` "USDC Cash <address>", created with the wallet; agents from before sign in (research-agent, the demo) credit the shared 1010. Unbalanced entries are rejected before anything is written.
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

The demo wallet's opening USDC balance is booked once as Dr 1010 / Cr 3000 so the trial balance is complete. Funding a user's wallet is not booked yet, so a user's cash account goes negative in the books as they pay.

## Database notes

- **Clock:** time windows (7 day budget, 10 minute duplicate check) and every timestamp use the database clock. Inside one transaction Postgres `now()` is frozen at the transaction start, so rows written in the same transaction share a timestamp; ties are broken by id.
- **Units:** money is stored as `numeric(18,6)` whole USDC (0.500000). The code works in integer atomic units (500000) and converts at the edges, so the guard and ledger logic never see decimals.
- **Integrity:** append only and balanced entries are enforced by the database for every connection.
- **Migrations:** tracked in `schema_migrations` and run only through `python -m sentinel.db migrate` (under an advisory lock, in one transaction). A database built before tracking (it has `payment_attempts` but no records) is recorded at the baseline 001 to 006 without rerunning them; 007 only changes rows and is safe to repeat, so it simply runs. The MCP server refuses to start while any migration is pending. The `test` schema picks new files up on the next `pytest`.

## Dashboard access

Row level security is on for every table. The anon role gets nothing. A signed in user can read the data (select only) only if their `app_metadata.role` is `dashboard_viewer`, which can be set server side only (`sentinel/migrations/005_dashboard_read_policies.sql`). `agents.token_hash` (left from the old agent tokens, now unused) is never readable from the browser. The five tables of migration 008 have RLS on and every grant revoked; the one new grant lets dashboard viewers read `agents.cash_account_id`.

    update auth.users set raw_app_meta_data = raw_app_meta_data || '{"role": "dashboard_viewer"}' where email = '...';

## Tests

Database tests run in the `test` schema of your Supabase database, each inside a rolled back transaction.

`test_guard.py` is the answer key for the guard: six planted cases (unknown seller, cap, budget, price jump, duplicate, clean) plus edge cases. `test_store.py` and `test_payments.py` cover the attempt log, the in flight lock and the books. `test_pay_flow.py` runs the whole payment flow against a fake seller and covers every way an approved payment can fail. `test_mcp_server.py` calls the tools with a token identity and checks each result the agent can see, plus `main()`'s startup checks. `test_oauth.py` covers discovery, registration, the full connect flow, consent, codes, refresh, revocation and access tokens. `test_web_auth.py` covers magic links, rate limits, sessions, CSRF, headers, the mailer and settings. `test_user_wallets.py` covers provisioning, isolation between users and failing closed. `test_migrations.py` covers tracked migrations and `test_app_smoke.py` the assembled app. `test_account_pages.py` covers the account pages over HTTP and `test_user_sellers.py` the two seller lists. `test_owner.py` covers the operator's seller list and limits the guard obeys, the ledger queries and the CSV. `test_ledger_app.py` covers the password gate, the forms, escaping and the CSV download. `test_wallets.py` covers wallet selection and shows that a signer other than CDP plugs into the same interface.

## Not built (by design)

No per payment human approval. The owner approves sellers, not payments: an unlisted seller is blocked outright, and the user (or the operator, for everyone) adds it if it is legitimate. The `approvals` table and the `pending` / `denied` attempt statuses remain in the schema unused (migration 007 closed out the old pending rows).

Not built yet: month end flags and the QuickBooks export.

## Known limits

- **Fetch hardening for `pay`:** pinning the fetch to the checked IP (DNS rebinding), a response size cap, an overall deadline, and holding a database connection only around database steps. Until it lands, do not open sign up to untrusted users.
- **Blocking CDP signer:** each signature is a blocking CDP call inside the event loop. An async x402 signer would fix it.
- **Tailwind:** the browser build compiles CSS at runtime (pinned with SRI).
- **Funding:** a new user's cash account goes negative in the books until funding is booked.
- **Ledger page:** the operator's page shows `user-<uuid>` agents without their email yet.
- **CIMD:** client ID metadata documents are not supported yet; registration is DCR only.
- **Fresh sign in:** consent does not yet require a recent sign in.
