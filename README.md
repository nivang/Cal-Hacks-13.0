# Sentinel

Agents can spend money now. Sentinel makes sure they spend it the way you meant and gives your accountant the books to prove it.

Sentinel is an MCP server that holds the wallet so the agent never does. Every payment is checked against your rules before it is paid and booked after. It pays in testnet USDC on Base Sepolia, so no real money moves. Design details: [docs/how-it-works.md](docs/how-it-works.md).

## You need

- Python 3.13 and [Claude Desktop](https://claude.ai/download)
- A [Coinbase Developer Platform](https://portal.cdp.coinbase.com) API key and wallet secret, plus three EVM accounts named seller-a, seller-b, seller-c (copy their addresses)
- A [Supabase](https://supabase.com) project (copy its pooled connection string)

## Setup

```bash
git clone https://github.com/nivang/Cal-Hacks-13.0.git && cd Cal-Hacks-13.0
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # fill in the CDP keys, seller addresses, DATABASE_URL; set SENTINEL_ALLOW_LOCAL_URLS=1
python -m sentinel.db migrate                              # builds the tables
python -m sentinel.auth issue research-agent --env-file .env   # writes the agent token into .env
```

Fund the wallet with Base Sepolia USDC at [faucet.circle.com](https://faucet.circle.com) (or `python -m demo.fund`). The wallet is the CDP account `sentinel`, created on first run.

Smoke test, which also approves the demo sellers and books the opening balance:

```bash
uvicorn demo.sellers:app --port 4000     # terminal 1: test sellers, keep running
python -m demo.run buy                   # terminal 2: should print PAID and a basescan link
```

## Add to Claude Desktop

Open Claude Desktop, Settings, Developer, Edit Config (or edit `~/Library/Application Support/Claude/claude_desktop_config.json`; on Windows `%APPDATA%\Claude\claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "sentinel": {
      "command": "/ABSOLUTE/PATH/TO/Cal-Hacks-13.0/.venv/bin/python",
      "args": ["-m", "sentinel.mcp_server"]
    }
  }
}
```

`echo "$PWD/.venv/bin/python"` in the repo prints the path to use. Secrets stay in `.env`; the server finds it on its own. Restart Claude Desktop, keep the test sellers running, and ask: *"Buy http://localhost:4000/benchmarks/email, I need email benchmarks for the Q3 report."*

Claude Code instead: `claude mcp add sentinel -- "$PWD/.venv/bin/python" -m sentinel.mcp_server`

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

- Tools missing in Claude Desktop: check `~/Library/Logs/Claude/mcp-server-sentinel.log`.
- "not a valid agent token": rerun `python -m sentinel.auth issue research-agent --env-file .env` and restart Claude Desktop.
- BLOCKED (seller): that seller is not approved. Approve it on the ledger page.

## Development

```bash
pytest          # database tests use the "test" schema of your Supabase database
ruff check .
mypy sentinel demo tests
```
