"""Command line demo of the three guard moments against the test site (test_site/app.py).

    python -m demo.run seed  approve CorpData and OpenWire for every agent and book the opening balance; pays nothing
    python -m demo.run buy   Acme Robotics' funding history for $0.10 from approved CorpData: ALLOW, settles on Base
                             Sepolia
    python -m demo.run surge CorpData suddenly quotes 4x the usual price: BLOCK (price_jump; run buy first)
    python -m demo.run scam  PremiumIntel's $9 dossier, as the newsroom's hidden text pushes: BLOCK (an unapproved
                             seller: the per payment cap, or no escrow offer from an unapproved seller)
    python -m demo.run intent  a task to brief the owner on Acme Robotics (read by Claude): the funding history fits
                               and is paid, then the approved OpenWire's crypto signals do not fit: BLOCK (off_task)
    python -m demo.run overpriced  the same briefing expects about $0.03 a source, so a $0.12 court search is
                               blocked: BLOCK (per_call)
    python -m demo.run escrow  CorpData offers escrow: $0.08 for the headcount trend is held, the data passes the
                               delivery check, captured
    python -m demo.run void    CorpData's docket monitor sends an HTML error page where it promised JSON: voided
    python -m demo.run escrow-refund  the briefing buys headcount by department and gets the weather in its fields:
                               Claude cannot vouch for it, so Sentinel voids the escrow and the money comes back
    python -m demo.run compare compare Acme Robotics news sources, then buy the best value for the briefing

Also seeds the approved sellers and the opening balance on first run. Real testnet payments, no simulation.
Pays as research-agent from the shared test wallet (demo.DEMO_ACCOUNT), which is created on first use.
intent, overpriced, escrow-refund and compare need ANTHROPIC_API_KEY: Claude reads the task and judges the payments.
compare also needs SENTINEL_ALLOW_LOCAL_URLS=1 and SENTINEL_DISCOVERY_URL pointing at the test site's catalog, and
searches the public directories too.
The escrow modes also open the escrow operator (escrow.open_operator), which pays the gas.
"""
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv
from x402.mechanisms.evm import ClientEvmSigner

from demo import DEMO_ACCOUNT
from sentinel import (
    ENV_FILE,
    apify,
    attempts,
    chain,
    compare,
    db,
    discovery,
    escrow,
    judge,
    ledger,
    mandates,
    owner,
    results,
    wallets,
)
from sentinel.payments import (
    NETWORK,
    USDC_BASE_SEPOLIA,
    PaymentRejected,
    Task,
    pay_for,
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
SCENARIOS = ("seed", "buy", "surge", "scam", "intent", "overpriced", "escrow", "void", "escrow-refund", "compare")
COMPARE_TASK = "Find recent news about Acme Robotics, with summaries and source links; keep data costs under $1."
COMPARE_REASON = "Need recent company news and sources for the Acme Robotics briefing; compare the offers."


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


async def start_task(the_judge: judge.Judge, text: str, per_call: int | None = None,
                     found_at: str = "owner") -> Task:
    """Have Claude read the task into a mandate for research-agent with its $1 budget (live at once).

    The per call estimate is set here (not left to the model), so the overpriced scenario always blocks on it and the
    others never do: tier 0's per_call rule runs before the off_task check.
    """
    draft = await the_judge.extract(text)
    print(f"TASK     {draft.goal} | may buy: {', '.join(draft.expected_types)} | "
          f"out of scope: {', '.join(draft.out_of_scope)}")
    with db.connection() as conn, db.transaction(conn):
        aid = db.agent_id(conn, AGENT)
        task_id = mandates.create(conn, aid, text, draft.goal, draft.expected_types, draft.out_of_scope, 1_000_000,
                                  per_call)
    return Task(task_id, found_at, the_judge)


async def compare_news(task: Task) -> results.ComparisonResult:
    """Compare every seller of Acme Robotics news for the task and print the table, best value first."""
    with db.connection() as conn:
        auto = attempts.load_agent(conn, AGENT)["auto_buy_best_value"]
    print(f"COMPARE  Acme Robotics news (auto buy {'on' if auto else 'off'})")
    async with httpx.AsyncClient(timeout=discovery.TIMEOUT_S, follow_redirects=False) as client:
        found = await compare.compare(db.connection, AGENT, task, "Acme Robotics news", 1, 50,
                                      fetch=discovery.http_fetcher(client, apify.headers_for),
                                      apify_credit=apify.has_credit(), purpose="Recent company news for the briefing.")
    for o in found.offers:
        mark = "*" if o.recommended else " "
        cost = f"${o.job_cost_usdc}" if o.job_cost_usdc else "?"
        quality = o.quality if o.quality is not None else "-"
        print(f"  {mark} {o.name[:34]:<34} {o.source:<8} {cost:>8}  q {quality!s:>3}  {o.note or o.fit_reason or ''}")
    print(f"PICK     {found.message}")
    return found


async def set_auto_buy(on: bool) -> None:
    """Flip research-agent's auto buy, as the owner would on the Rules page."""
    with db.connection() as conn, db.transaction(conn):
        owner.set_auto_buy(conn, AGENT, on)


async def run_compare(signer: ClientEvmSigner) -> None:
    """Compare marketplace news sources; demonstrate auto buy when the best value is the pricier option."""
    await set_auto_buy(False)
    task = await start_task(judge.ClaudeJudge(), COMPARE_TASK, None, "sentinel discovery")
    found = await compare_news(task)
    if found.best_buyable is None:
        return
    await attempt(signer, found.best_buyable.url, COMPARE_REASON, task)
    if not found.needs_owner:
        return  # An allowed/free pick was already fetched; do not pay for it twice.
    print("OWNER    turns auto buy on")
    await set_auto_buy(True)
    try:
        found = await compare_news(task)
        if found.best_buyable is not None:
            await attempt(signer, found.best_buyable.url, COMPARE_REASON, task)
    finally:
        await set_auto_buy(False)


async def attempt(
    signer: ClientEvmSigner, path: str, reason: str, task: Task | None = None,
    operator: escrow.EscrowOperator | None = None,
) -> None:
    """Pay for one path (or a full URL) and print the outcome."""
    url = path if path.startswith(("http://", "https://")) else f"{SELLERS_URL}{path}"
    try:
        result = await pay_for(url, signer, agent=AGENT, reason=reason, task=task,
                               operator=operator)
    except PaymentRejected as e:
        print(f"BLOCKED  {e}")
        return
    if result.verdict is not None:
        print(f"CHECKED  {result.verdict.outcome} ({result.verdict.rule}): {result.verdict.detail}")
    if result.escrow == "voided":
        print("REFUNDED the money went back, the data is withheld")
    else:
        print(f"PAID     HTTP {result.status} {result.body}" + (" (captured from escrow)" if result.escrow else ""))
    if result.tx_hash:
        print(f"https://sepolia.basescan.org/tx/{result.tx_hash}")


async def run_escrow(scenario: str, signer: ClientEvmSigner) -> None:
    """The escrow scenarios: the operator holds the money until the delivery is checked."""
    async with escrow.open_operator() as operator:
        if scenario == "escrow":
            await attempt(signer, f"{API}/company/headcount{ACME}", "demo escrow", operator=operator)
        elif scenario == "void":
            await attempt(signer, f"{API}/legal/docket-monitor{ACME}", "demo escrow void", operator=operator)
        else:
            task = await start_task(judge.ClaudeJudge(), TASK)
            await attempt(signer, f"{API}/company/headcount-by-team{ACME}", "The owner's Acme Robotics briefing asks "
                          "for the headcount trend; a split by department shows where it changed.", task, operator)


async def run(scenario: str) -> None:
    """Run one demo scenario and print what the guard (and intent binding) decided, then the trial balance."""
    paths = {"buy": FUNDING, "surge": FUNDING, "scam": f"{API}/intel/deep-dossier{ACME}"}
    async with wallets.cdp_wallet(DEMO_ACCOUNT, create=True) as signer:
        with db.connection() as conn:
            db.init(conn)
            await bootstrap_books(conn, signer.address)
        if scenario == "seed":
            print("Approved CorpData and OpenWire for every agent. QuickScrape's fresh wallet stays unapproved, so")
            print(f"Sentinel pays {FRESH_WALLET} only through escrow.")
        elif scenario in ("escrow", "void", "escrow-refund"):
            await run_escrow(scenario, signer)
        elif scenario == "compare":
            await run_compare(signer)
        elif scenario == "intent":
            task = await start_task(judge.ClaudeJudge(), TASK)
            await attempt(signer, FUNDING, REASON, task)
            await attempt(signer, f"{API}/crypto/signals", "Crypto market sentiment could add color to the briefing.",
                          task)
        elif scenario == "overpriced":
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
