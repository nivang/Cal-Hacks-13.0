"""Command line demo of the three guard moments against the test site (test_site/app.py).

    python -m demo.run seed  approve CorpData and OpenWire for every agent and book the opening balance; pays nothing
    python -m demo.run buy   Acme Robotics' funding history for $0.10 from approved CorpData: ALLOW, settles on Base
                             Sepolia
    python -m demo.run surge CorpData suddenly quotes 4x the usual price: BLOCK (price_jump; run buy first)
    python -m demo.run scam  PremiumIntel's $9 dossier, as the newsroom's hidden text pushes: BLOCK (seller)
    python -m demo.run intent  a task to brief the owner on Acme Robotics (read by Claude): the funding history fits
                               and is paid, then the approved OpenWire's crypto signals do not fit: BLOCK (off_task)
    python -m demo.run held    the same briefing expects about $0.03 a source, so a $0.12 court search is held: ESCALATE
    python -m demo.run escrow  CorpData offers escrow: $0.08 for the headcount trend is held, the data passes the
                               delivery check, captured
    python -m demo.run void    CorpData's docket monitor sends an HTML error page where it promised JSON: voided
    python -m demo.run escrow-held  the briefing buys headcount by department and gets the weather in its fields:
                               Claude cannot vouch for it, so it stays in escrow for the owner, who then refunds it here

Also seeds the approved sellers and the opening balance on first run. Real testnet payments, no simulation.
Pays as research-agent from the shared test wallet (demo.DEMO_ACCOUNT), which is created on first use.
intent, held and escrow-held need ANTHROPIC_API_KEY: Claude reads the task and judges the payments.
The escrow modes also open the escrow operator (escrow.open_operator), which pays the gas.
"""
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv
from x402.mechanisms.evm import ClientEvmSigner

from demo import DEMO_ACCOUNT
from sentinel import ENV_FILE, attempts, chain, db, escrow, judge, ledger, mandates, wallets
from sentinel.payments import (
    NETWORK,
    USDC_BASE_SEPOLIA,
    PaymentHeld,
    PaymentRejected,
    Task,
    pay_for,
    settle_held,
)
from test_site import FRESH_WALLET

SELLERS_URL = "http://localhost:4000"
AGENT = "research-agent"
TASK = ("I'm meeting Acme Robotics next week. Pull their funding history, headcount trend, recent news, and any "
        "litigation or regulatory filings. Use paid sources only if the free ones don't cover it. Budget $1, and give "
        "me a briefing with sources.")
REASON = "The free profile has no funding data; the owner's Acme Robotics briefing needs its funding history."
API = "/test-site/api"
ACME = "?company=Acme+Robotics"
FUNDING = f"{API}/company/funding{ACME}"
APPROVED = {"CorpData": "SELLER_A_ADDRESS", "OpenWire": "SELLER_B_ADDRESS"}
SCENARIOS = ("seed", "buy", "surge", "scam", "intent", "held", "escrow", "void", "escrow-held")


async def bootstrap_books(conn: db.Connection, wallet_address: str) -> None:
    """Seed the approved sellers from env and book the wallet's current USDC as owner funding, once.

    Rows seeded earlier under other names (DataCo, CartCo...) keep their status and take the marketplace's names.
    """
    db.seed_sellers(conn, [
        {"name": name, "pay_to": os.environ[var], "network": NETWORK, "asset": USDC_BASE_SEPOLIA, "status": "approved"}
        for name, var in APPROVED.items()
    ])
    for name, var in APPROVED.items():
        conn.execute("UPDATE sellers SET name = %s WHERE agent_id IS NULL AND lower(pay_to) = lower(%s)",
                     (name, os.environ[var]))
    if not ledger.has_entries(conn):
        balance = await chain.usdc_balance(wallet_address)
        ledger.post_funding(conn, balance, "Opening balance: USDC already in sentinel wallet")


async def set_price(path: str, price: str) -> None:
    """Change a test seller's price live through its admin endpoint (demo fixture only)."""
    async with httpx.AsyncClient() as http:
        await http.post(f"{SELLERS_URL}/test-site/admin/price", json={"path": path, "price": price})


async def start_task(the_judge: judge.Judge, text: str, per_call: int | None = None) -> Task:
    """Have Claude read the task into a mandate for research-agent and confirm it with its $1 budget, as the owner
    would.

    The per call estimate is set here (not left to the model), so the held scenario always holds and the others never
    hold on it: tier 0's per_call rule runs before the off_task check.
    """
    draft = await the_judge.extract(text)
    print(f"TASK     {draft.goal} | may buy: {', '.join(draft.expected_types)} | "
          f"out of scope: {', '.join(draft.out_of_scope)}")
    with db.connection() as conn, db.transaction(conn):
        aid = db.agent_id(conn, AGENT)
        task_id = mandates.create(conn, aid, text, draft.goal, draft.expected_types, draft.out_of_scope, 1_000_000,
                                  per_call)
        mandates.confirm(conn, task_id, aid, 1_000_000, attempts.load_agent(conn, AGENT)["weekly_budget"])
    return Task(task_id, "owner", the_judge)


async def attempt(
    signer: ClientEvmSigner, path: str, reason: str, task: Task | None = None,
    operator: escrow.EscrowOperator | None = None,
) -> int | None:
    """Pay for one path and print the outcome. Returns the request id of a payment left in escrow for the owner."""
    try:
        result = await pay_for(f"{SELLERS_URL}{path}", signer, agent=AGENT, reason=reason, task=task,
                               operator=operator)
    except PaymentHeld as e:
        print(f"HELD     {e}")
        return None
    except PaymentRejected as e:
        print(f"BLOCKED  {e}")
        return None
    if result.verdict is not None:
        print(f"CHECKED  {result.verdict.outcome} ({result.verdict.rule}): {result.verdict.detail}")
    if result.escrow == "held":
        print("ESCROW   held for the owner, the seller is not paid yet")
        return result.attempt_id
    if result.escrow == "voided":
        print("REFUNDED the money went back, the data is withheld")
    else:
        print(f"PAID     HTTP {result.status} {result.body}" + (" (captured from escrow)" if result.escrow else ""))
    if result.tx_hash:
        print(f"https://sepolia.basescan.org/tx/{result.tx_hash}")
    return None


async def run_escrow(scenario: str, signer: ClientEvmSigner) -> None:
    """The escrow scenarios: the operator holds the money until the delivery is checked."""
    async with escrow.open_operator() as operator:
        if scenario == "escrow":
            await attempt(signer, f"{API}/company/headcount{ACME}", "demo escrow", operator=operator)
        elif scenario == "void":
            await attempt(signer, f"{API}/legal/docket-monitor{ACME}", "demo escrow void", operator=operator)
        else:
            task = await start_task(judge.ClaudeJudge(), TASK)
            held = await attempt(signer, f"{API}/company/headcount-by-team{ACME}", "The owner's Acme Robotics briefing "
                                 "asks for the headcount trend; a split by department shows where it changed.", task,
                                 operator)
            if held is not None:
                with db.connection() as conn:
                    outcome = await settle_held(conn, operator, held, db.agent_id(conn, AGENT), False)
                print(f"OWNER    refunds it: {outcome}")


async def run(scenario: str) -> None:
    """Run one demo scenario and print what the guard (and intent binding) decided, then the trial balance."""
    paths = {"buy": FUNDING, "surge": FUNDING, "scam": f"{API}/intel/deep-dossier{ACME}"}
    async with wallets.cdp_wallet(DEMO_ACCOUNT, create=True) as signer:
        with db.connection() as conn:
            db.init(conn)
            await bootstrap_books(conn, signer.address)
        if scenario == "seed":
            print("Approved CorpData and OpenWire for every agent. To see a fresh wallet held, approve QuickScrape on")
            print(f"the Rules page at {FRESH_WALLET}")
        elif scenario in ("escrow", "void", "escrow-held"):
            await run_escrow(scenario, signer)
        elif scenario == "intent":
            task = await start_task(judge.ClaudeJudge(), TASK)
            await attempt(signer, FUNDING, REASON, task)
            await attempt(signer, f"{API}/crypto/signals", "Crypto market sentiment could add color to the briefing.",
                          task)
        elif scenario == "held":
            task = await start_task(judge.ClaudeJudge(), TASK, 30_000)
            await attempt(signer, f"{API}/legal/court-records{ACME}", "The owner's Acme Robotics briefing asks for any "
                          "litigation; the free sources have no court records.", task)
        else:
            if scenario == "surge":
                await set_price(FUNDING.split("?")[0], "$0.40")
            try:
                await attempt(signer, paths[scenario], f"demo {scenario}")
            finally:
                if scenario == "surge":
                    await set_price(FUNDING.split("?")[0], "$0.10")

    with db.connection() as conn:
        for row in ledger.trial_balance(conn):
            print(f"{row['code']} {row['name']:<30} Dr {row['debit']:>9} Cr {row['credit']:>9}")


if __name__ == "__main__":
    load_dotenv(ENV_FILE)
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        sys.exit(__doc__)
    try:
        asyncio.run(run(sys.argv[1]))
    finally:
        db.close_pool()
