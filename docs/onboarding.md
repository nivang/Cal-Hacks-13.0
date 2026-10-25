# Onboarding: sign in, connect Claude, pay

One process (`python -m sentinel.mcp_server`) serves the sign in site, the OAuth server and the MCP endpoint on one origin, `PUBLIC_BASE_URL` (default `http://127.0.0.1:8000` when you run it yourself). The MCP URL is that origin plus `/mcp`. How it works inside: [how-it-works.md](how-it-works.md#onboarding-and-identity).

## What a user does

1. Open the site and sign in with an email address. Sentinel mails a link (on a local run it prints it in the server's terminal). The link works once and expires in 15 minutes; opening it shows a "Sign in as ..." button, and only that button spends it, so mail scanners cannot.
2. The first sign in creates the user's own wallet: a CDP server account named by their user id, plus their own agent (`user-<uuid>`) and cash account in the books, with the default limits ($10 per payment, $50 per week). The account page shows the wallet address with a Basescan link. If Coinbase does not answer in time, the user stays signed in and the account page offers **Create my wallet** to retry.
3. Fund the wallet with Base Sepolia USDC (see [Funding](#funding)); the Overview page has a **Fund it** button.
4. Open **Rules** on the account page and approve the sellers the agent may pay (a name and the address from the vendor), and change the per payment cap and weekly budget if the defaults do not suit. Unlisted sellers instead require a one purchase Yes/No approval; explicit blocks always stop payment.
5. Add Sentinel to the AI app with the MCP URL from the account page. The app opens a browser tab; the user signs in if needed and sees a consent page naming where the approval goes. **Allow** connects the app; **Deny** tells it no.
6. From then on every `pay` call signs with that user's own wallet, under the same guard rules, within that user's limits, and only to approved sellers or a specific purchase the user approves in the native form or authenticated browser page. The **Payments** and **Books** pages show what happened, and the Overview checklist ticks the three steps off.

**Sign out everywhere** on the account page ends every browser session and disconnects every app the user approved at once. An app reconnects by going through consent again.

## claude.ai or Claude Desktop: custom connector

Needs a public HTTPS deploy (claude.ai connects from Anthropic's servers, not your machine).

1. Settings, Connectors, Add custom connector.
2. URL: `https://<host>/mcp`. Leave the OAuth client id and secret fields empty: Claude registers itself.
3. Connect, then sign in and Allow in the tab that opens.

## Claude Code

```bash
claude mcp add --transport http sentinel https://<host>/mcp     # or http://localhost:8000/mcp locally
```

Then run `/mcp` inside Claude Code, pick sentinel, authenticate, and Allow in the browser. The account page shows this exact command with your URL.

## Claude Desktop with a local server

Custom connectors cannot reach `localhost`, so a local server goes through `mcp-remote`, which runs the OAuth flow on your machine. Pin a version: 0.1.16 or later (earlier ones have CVE-2025-6514, a command injection through a malicious server's metadata). 0.1.29 (used below) supports `--auth-timeout`.

1. Start the server and sign in on the site first (`http://localhost:8000`), so the consent page opens straight away instead of racing the timeout through a sign in email.
2. Settings, Developer, Edit Config (or edit `~/Library/Application Support/Claude/claude_desktop_config.json`; on Windows `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sentinel": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.1.29", "http://localhost:8000/mcp", "--auth-timeout", "300"]
    }
  }
}
```

3. Restart Claude Desktop and Allow in the tab that opens. `mcp-remote` keeps its tokens in `~/.mcp-auth`; delete that folder to connect again from scratch.

## MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

Transport **Streamable HTTP**, URL `http://localhost:8000/mcp`, then use the OAuth flow in its auth panel. Direct connection from the browser works: `/mcp`, `/register`, `/token`, `/revoke` and the discovery documents answer CORS requests, and the Inspector's callback on `http://localhost:6274` is a loopback redirect, which Sentinel accepts on any port.

## Local run

```bash
source .venv/bin/activate
cp .env.example .env    # the CDP keys, DATABASE_URL, ANTHROPIC_API_KEY, the two secrets; PUBLIC_BASE_URL=http://localhost:8000
python -m sentinel.db migrate
python -m sentinel.mcp_server                 # site, OAuth and MCP on http://127.0.0.1:8000
```

Dev mode means `PUBLIC_BASE_URL` and the bind host are both loopback. Only then may `RESEND_API_KEY` and `MAIL_FROM` stay empty: each sign in link prints to the server's stderr as `Sign in link for <email>: <link>`. Only then is `SENTINEL_ALLOW_LOCAL_URLS=1` allowed, which lets `pay` reach the test sellers (`uvicorn test_site.app:app --port 4000`). Those sellers can be paid once approved: `python -m demo.run seed` approves CorpData and OpenWire, or each user approves them on the Rules page by address.

Open the site at the host named in `PUBLIC_BASE_URL` (`localhost` and `127.0.0.1` are different origins to a browser; a form posted from the other one is refused).

`curl -si -X POST http://localhost:8000/mcp` shows the 401 challenge a client starts from.

## Deploy

- Production uses its own CDP project and its own database. Never put production CDP keys in a developer `.env`.
- Do not open sign up to untrusted users until the `pay` fetch hardening lands (next change; see [Known limits](how-it-works.md#known-limits)).
- Public HTTPS with an IPv4 record (DNS A). Allow Anthropic's egress range `160.79.104.0/21` through any firewall: claude.ai connects and registers from there.
- `FORWARDED_ALLOW_IPS` set to the proxy's address, so client IPs and the per IP limits are real (`*` lets anyone spoof an IP; unset, every request seems to come from the proxy, so the per IP limits become site wide and the Anthropic registration exemption never matches; both only warn at startup). Strip query strings in the proxy's logs: sign in links and consent requests travel in them. Sentinel already strips them from its own access log.
- `ANTHROPIC_API_KEY` for the intent judge (Claude Haiku 5.5). Without it the server will not start; if the API is down, payments under a task are held for the owner, never paid.
- A Resend domain, with `RESEND_API_KEY` and `MAIL_FROM` on it. Give each deployment and database its own Resend key (the idempotency keys are per link id, which repeats across databases).
- Run `python -m sentinel.db migrate` before starting a new version; the server refuses to start while migrations are pending.
- Use sticky routing for legacy MCP sessions when running several replicas; see [link approvals](link-approvals.md). Otherwise, sessions, links, codes, tokens and the rate limits all live in Postgres. The in flight payment limits (2 per user, 6 overall) are per process.

Start it with `python -m sentinel.mcp_server --host 0.0.0.0` (it reads `PORT` when the platform sets one). `GET /healthz` is a liveness check that does not touch the database. The operator ledger page (`sentinel.ledger_app`, port 5001: macOS uses 5000 for AirPlay) is a separate process with its own password; users do not need it.

## Funding

Every user funds their own wallet: [faucet.circle.com](https://faucet.circle.com), pick Base Sepolia, paste the address from the account page. It is testnet USDC; no real money moves. Funding is not booked yet, so a new user's cash account shows a negative balance in the books until it is.

## Troubleshooting

- **"Missing environment variables: ..."**: the server names every one it needs. Outside localhost that includes `RESEND_API_KEY` and `MAIL_FROM`.
- **"Sentinel cannot start"**: a value is wrong, and each problem is listed (http outside localhost, a secret under 32 characters, the two secrets equal, `SENTINEL_ALLOW_LOCAL_URLS` outside localhost).
- **"The database is missing migrations"**: run `python -m sentinel.db migrate`.
- **No sign in email locally**: look in the server's terminal for `Sign in link for ...`.
- **"Too many sign in links"** (429): 3 links per address per 15 minutes and 10 per day, 60 per IP per hour, 300 overall per hour. Wait for the time shown.
- **"That request was refused"** (403) on a form: the page was opened on a different origin than `PUBLIC_BASE_URL`, or it is stale. Reload it from the right host.
- **"Finish connecting in the other browser"**: consent only works in the browser that started the connection. Start again from the app in this browser.
- **"Your wallet is not ready yet"**: Coinbase did not answer at sign in. Use **Create my wallet** on the account page, then connect again.
- **The app's registration is refused**: `/register` takes `Content-Type: application/json` only (415 otherwise). Redirect URIs must be https, http on exactly `localhost`, `127.0.0.1` or `[::1]`, or a private app scheme; never Sentinel's own host, a browser scheme that wraps another URL, a fragment, userinfo or a non loopback IP.
- **An error page instead of a redirect at /authorize**: Sentinel sends errors back only to Claude's own callbacks and loopback; any other app gets the page, so `/authorize` is never an open redirector.
- **The app gets 401s or asks to connect again**: the grant was revoked (sign out everywhere, a replayed code or a reused refresh token) or is older than 90 days. Access tokens last an hour and the app refreshes them on its own; when the grant is gone, connect again.
- **FAILED "could not open your wallet"**: CDP failed or did not answer in time (about 15 seconds). Nothing was paid; try again.
- **APPROVAL_REQUIRED**: answer the native form, or open `approval_url` and sign in. After browser approval call `resume_payment(request_id)`. See [link approvals](link-approvals.md).
- **BLOCKED (seller)**: an applicable seller block cannot be overridden by a purchase approval.
- **Claude Desktop with mcp-remote times out**: sign in on the site first and keep `--auth-timeout 300`.
