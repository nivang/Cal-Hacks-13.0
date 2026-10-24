"""Command line demo of the three guard moments against the local test sellers (demo/sellers.py).

    python -m demo.run buy   clean $0.50 purchase from approved DataCo: ALLOW, settles on Base Sepolia
    python -m demo.run surge DataCo suddenly quotes 4x the usual price: BLOCK (price_jump)
    python -m demo.run scam  a quote from an unlisted seller, as an injected page would push: BLOCK (seller)
    python -m demo.run intent  a task for the Q3 email report (read by Claude): DataCo's benchmarks fit and are paid,
                               then the approved InsightLab's crypto signals do not fit: BLOCK (off_task)
    python -m demo.run held    the same task expects about $0.10 a purchase, so a $1.00 report is held: ESCALATE

Also seeds the approved sellers and the opening balance on first run. Real testnet payments, no simulation.
Pays as research-agent from the shared test wallet (demo.DEMO_ACCOUNT), which is created on first use.
intent and held need ANTHROPIC_API_KEY: Claude reads the task and judges the payments.
"""
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv
from x402.mechanisms.evm import ClientEvmSigner

from demo import DEMO_ACCOUNT
from sentinel import ENV_FILE, attempts, chain, db, judge, ledger, mandates, wallets
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA, PaymentHeld, PaymentRejected, Task, pay_for

SELLERS_URL = "http://localhost:4000"
AGENT = "research-agent"
TASK = "Write the Q3 email marketing report. Buy the 2026 email open-rate benchmarks; about $0.10 per data purchase."
REASON = "Need 2026 email open-rate benchmarks for the Q3 email marketing report the owner asked for."
SCENARIOS = ("buy", "surge", "scam", "intent", "held")


async def bootstrap_books(conn: db.Connection, wallet_address: str) -> None:
    """Seed the approved sellers from env and book the wallet's current USDC as owner funding, once."""
    approved = {"DataCo": "SELLER_A_ADDRESS", "InsightLab": "SELLER_B_ADDRESS"}
    db.seed_sellers(conn, [
        {"name": name, "pay_to": os.environ[var], "network": NETWORK, "asset": USDC_BASE_SEPOLIA, "status": "approved"}
        for name, var in approved.items()
    ])
    if not ledger.has_entries(conn):
        balance = await chain.usdc_balance(wallet_address)
        ledger.post_funding(conn, balance, "Opening balance: USDC already in sentinel wallet")


async def set_price(path: str, price: str) -> None:
    """Change a test seller's price live through its admin endpoint (demo fixture only)."""
    async with httpx.AsyncClient() as http:
        await http.post(f"{SELLERS_URL}/admin/price", json={"path": path, "price": price})


async def start_task(the_judge: judge.Judge) -> Task:
    """Have Claude read TASK into a mandate for research-agent and confirm it with a $5 budget, as the owner would.

    The per call estimate is set to $0.10 here (not left to the model), so the held scenario always holds.
    """
    draft = await the_judge.extract(TASK)
    print(f"TASK     {draft.goal} | may buy: {', '.join(draft.expected_types)} | "
          f"out of scope: {', '.join(draft.out_of_scope)}")
    with db.connection() as conn, db.transaction(conn):
        aid = db.agent_id(conn, AGENT)
        task_id = mandates.create(conn, aid, TASK, draft.goal, draft.expected_types, draft.out_of_scope, 5_000_000,
                                  100_000)
        mandates.confirm(conn, task_id, aid, 5_000_000, attempts.load_agent(conn, AGENT)["weekly_budget"])
    return Task(task_id, "owner", the_judge)


async def attempt(signer: ClientEvmSigner, path: str, reason: str, task: Task | None = None) -> None:
    """Pay for one path and print the outcome."""
    try:
        result = await pay_for(f"{SELLERS_URL}{path}", signer, agent=AGENT, reason=reason, task=task)
        print(f"PAID     HTTP {result.status} {result.body}")
        print(f"https://sepolia.basescan.org/tx/{result.tx_hash}")
    except PaymentHeld as e:
        print(f"HELD     {e}")
    except PaymentRejected as e:
        print(f"BLOCKED  {e}")


async def run(scenario: str) -> None:
    """Run one demo scenario and print what the guard (and intent binding) decided, then the trial balance."""
    paths = {"buy": "/benchmarks/email", "surge": "/benchmarks/email", "scam": "/premium"}
    async with wallets.cdp_wallet(DEMO_ACCOUNT, create=True) as signer:
        with db.connection() as conn:
            db.init(conn)
            await bootstrap_books(conn, signer.address)
        if scenario in ("intent", "held"):
            task = await start_task(judge.ClaudeJudge())
            if scenario == "intent":
                await attempt(signer, "/benchmarks/email", REASON, task)
                await attempt(signer, "/signals/crypto", "Need market signals for the report.", task)
            else:
                await attempt(signer, "/reports/social", "Need social engagement rates to compare with email.", task)
        else:
            if scenario == "surge":
                await set_price("/benchmarks/email", "$2.00")
            try:
                await attempt(signer, paths[scenario], f"demo {scenario}")
            finally:
                if scenario == "surge":
                    await set_price("/benchmarks/email", "$0.50")

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
