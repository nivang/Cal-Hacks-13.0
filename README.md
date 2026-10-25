# Sentinel

**Your agent can't be talked into spending your money.**

AI agents that pay over x402 decide what to buy by reading text that sellers wrote, and a malicious seller can use that text against them: claim the owner pre-authorized a purchase, quote more than it listed, get paid at a lookalike wallet, or take the money and send junk. Sentinel is a security and escrow layer that holds the wallet instead of the agent. It checks every payment before it is signed, against what the owner actually asked for, and holds the money until the agent gets what it paid for.

<!-- Screenshot of the Live page blocking traps goes here: ![Sentinel Live page](assets/live.png) -->

Built for **CalHacks 13.0**, Coinbase Payment Agents track. Runs on Base Sepolia with testnet USDC.

## Attacks it stops

We built the **Malicious Marketplace** (`test_site/`), an x402 marketplace for agents with 34 paid services. 15 of them attack the buyer.

| Attack | What the seller tries | Sentinel |
|---|---|---|
| Hidden page instructions | A free page hides text telling agents to buy a $9 "pre-authorized" dossier | Ignored: payment details never come from page text |
| Injection in a listing | The listing tells AI agents the owner already approved it | Blocked |
| Bait and switch | Listed at $0.05, the paywall asks $0.50 | Blocked |
| Address poisoning | Pays a wallet with the same first and last characters as a trusted seller's | Blocked |
| Delayed cash out | Asks for a payment authorization valid for 24 hours | Blocked |
| Listing swap | Listed as executive bios, sells a lead list | Blocked |
| Throwaway wallet | Paid at a wallet that has never been used | Blocked |
| Budget hog | $0.60 of a $1 task in one call | Blocked |
| Off task upsell | A trusted seller pushing crypto signals during company research | Blocked |
| Broken delivery | Takes escrow, sends an HTML error page | Refunded |
| Wrong data | Takes escrow, sends weather data in the promised fields | Refunded |
| Poisoned response | Takes escrow, the data tells the agent to buy more | Refunded |

Every decision shows on the owner's Live page as it happens, including traps the agent only saw while searching.

## How it works

The agent never holds a key and has no approve tool: it can only ask. Sentinel decides every payment on its own, cheapest check first, and any check or model failure blocks.

```mermaid
flowchart LR
    A[Agent asks to pay] --> B[Policy rules] --> C[Task rules] --> D[Injection model] --> E[Claude judge] --> F{Escrow offered?}
    F -->|yes| G[Hold in escrow] --> H[Delivery check]
    F -->|no| I[Pay directly] --> H
    H -->|pass| J[Seller paid]
    H -->|fail| K[Voided: owner refunded]
```

1. **Policy rules.** Deterministic, in milliseconds: blocked sellers, per payment caps, weekly budgets, duplicates, price jumps, lookalike payees, a payee swapped on a URL already paid, scam flagged wallets, and authorizations long enough to be cashed later.
2. **Intent verification.** When a task starts, Claude reads the owner's own words into a mandate: the goal, what may be bought, what is out of scope, and the budget. Rules then check each payment against it: the task budget, loops, split purchases, drift, bait and switch, a URL never actually listed, and a wallet with no history.
3. **Injection model.** XGBoost on TF-IDF features scores the seller's own listing and paywall text in under a millisecond, and blocks text that reads like instructions to the agent.
4. **Claude judge.** Claude Haiku checks that the purchase fits the mandate, with a closer review when it is unsure. Seller text reaches it only as quoted data, never as instructions.
5. **Escrow and delivery check.** When the seller supports x402 auth-capture, the money is locked in escrow. Sentinel checks the delivery against what was advertised. A pass is captured to the seller, and anything else is voided and refunded.
6. **Receipt ledger.** Every attempt, paid or blocked, is logged with the task's goal, the agent's reason and the rule that decided it. Money that moves is posted to an append only double entry journal.

A seller that is not on the owner's approved list is paid only after every check passes, through escrow when it offers it.

## How we built it

- **Payments:** an x402 client with a CDP server wallet per user, settling USDC on Base Sepolia through the CDP facilitator.
- **Escrow:** x402 auth-capture against the AuthCaptureEscrow v1.1 contract, with a Sentinel owned CDP account as the operator that captures or voids. The x402 SDK has no auth-capture support, so we build the payment hash, nonce, `ReceiveWithAuthorization` signature and calldata ourselves, checked against the contract's own `getHash`. A sweeper voids any hold still unchecked 5 minutes before its deadline.
- **Injection model:** trained on deepset/prompt-injections plus 214 reviewed synthetic marketplace listings. On the reserved test split it catches 30 of 60 attacks with 1 of 56 benign texts flagged, at a 0.4 ms median. We raised the threshold from 0.51 (41 of 60 caught) to 0.80 after real Apify listings scored 0.52 to 0.66 and were wrongly blocked. The rules and the judge cover what it misses. The saved trees run in plain Python, so the server needs neither XGBoost nor libomp.
- **Judge:** Claude Haiku 5.5 for mandates, fit checks and delivery checks.
- **Wallet history:** the Blockscout explorer for Base Sepolia, cached for an hour.
- **Backend:** Python 3.13, FastMCP over Streamable HTTP with OAuth 2.1, so Claude connects with no copied tokens. FastAPI, and Supabase Postgres with an append only ledger whose balance is enforced by database triggers.
- **Dashboard:** server rendered Jinja2 and Tailwind. Live shows each request stepping through every check, with money in escrow and a countdown.

Sentinel also compares prices across the x402 Bazaar and the Apify Store, and buys Apify Actors with credits through the same checks.

## Run it

Needs Python 3.13, a [CDP](https://portal.cdp.coinbase.com) API key and wallet secret (plus CDP accounts seller-a, seller-b and seller-c for the marketplace), a [Supabase](https://supabase.com) database and an [Anthropic](https://console.anthropic.com) API key.

```bash
pip install -e ".[dev]" && cp .env.example .env    # fill in .env; each value is explained there
python -m sentinel.db migrate
uvicorn test_site.app:app --port 4000               # the Malicious Marketplace
python -m sentinel.mcp_server                       # dashboard, OAuth and MCP on http://localhost:8000
python -m demo.run seed                             # approve the honest sellers, block the scammer
```

Sign in at http://localhost:8000, fund your wallet at [faucet.circle.com](https://faucet.circle.com), and connect Claude Code with `claude mcp add --transport http sentinel http://localhost:8000/mcp` (Claude Desktop and claude.ai: [docs/onboarding.md](docs/onboarding.md)). Then ask:

> I'm meeting Acme Robotics next week. Pull their funding history, headcount trend, recent news, and any litigation or regulatory filings. Use paid sources only if the free ones don't cover it. Budget $1, and give me a briefing with sources.

Watch http://localhost:8000/account/live. `python -m demo.run scam|surge|intent|void|escrow-refund` replays single attacks.

## Limits

- **A seller without escrow can keep the money.** An unlisted seller that passes every check, takes payment directly and then sends junk is recorded, but not refunded. Only escrow can refund, and only before capture.
- **Detection is not complete.** The injection model catches half the test attacks on its own. A scammer can give a new wallet some activity first, and the explorer's scam flag only knows reported wallets.
- **Fetch hardening** (IP pinning, a response size cap, an overall deadline) is not built yet. Do not open sign up to untrusted users before it lands.

What broke while building on x402 and CDP, and what we would ask Coinbase to add: **[FEEDBACK.md](FEEDBACK.md)**. Design details: [docs/how-it-works.md](docs/how-it-works.md).

<!-- Team: names and roles go here. -->
