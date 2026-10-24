"""Tests for per user wallets: each user's own CDP account, their own books, and isolation between users.

What this file covers, by area. Items marked (HTTP) run through the whole app (conftest app and browser fixtures):
real sign in, real OAuth tokens, POST /mcp, with the fake wallet and the pay spy behind it.
- Wallet seam: the CDP opener looks up the account named by the user's uuid and creates it only when asked, never
  "sentinel"; ids that are not a uuid are refused before any CDP call; a slow lookup times out.
- Provisioning (web/users.py): first sign in (find_or_create_user, then ensure_wallet) creates exactly one agent and
  one cash account, with the default limits, and opens the wallet with create=True under the user's uuid;
  ensure_wallet and create_books are idempotent and create_books refuses a different address; a failed or timed
  out wallet open leaves no partial books; the database refuses shared wallets, agents and cash accounts.
- Users and sessions (web/users.py): emails are found case insensitively and mail goes to the address on file;
  Identity claims round trip and malformed claims are refused; session_user honours the epoch; sign out
  everywhere bumps it and revokes every live grant of that user only; the account page fields.
- Isolation: one agent's settled prices do not move another agent's price baseline; two users get different
  agents, signers and cash accounts, each settled payment credits its own user's account and week spend is per
  user. (HTTP) A token resolves to its own user's agent and wallet; two users signed in through the site pay from
  their own signers into their own cash accounts, cannot read each other's requests, do not share a budget and
  cannot move each other's price baseline.
- Fail closed (HTTP): missing, invalid, expired and revoked tokens never call the wallet opener and never write
  an attempt (401 on POST /mcp); an address mismatch, a missing or a failing wallet fails closed with no attempt;
  paying never creates a wallet; the per user in flight limit holds over HTTP (InFlight itself and the in memory
  client case are in test_mcp_server.py).
- Legacy: research-agent and the demo still credit the shared 1010; an agent with its own cash account is credited
  there and the trial balance still balances.

No network: CdpClient is replaced by a fake that records every call (wallet seam tests), and the other tests use
conftest's fake_wallets (a fake opener registered as wallets.WALLETS["fake"] with SENTINEL_WALLET=fake), so the real
open_user_wallet runs. Database tests run in the rolled back test transaction (conn fixture), and expected
constraint failures run inside db.transaction savepoints.
"""
import asyncio
import secrets
import time
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from conftest import (
    ASSET,
    DATACO,
    NETWORK,
    TEST_BASE_URL,
    PaySpy,
    call_tool,
    connect_flow,
)
from conftest import fake_wallet_address as address_for

from sentinel import attempts, db, guard, ledger, mcp_server, payments, wallets
from sentinel.guard import Quote
from sentinel.payments import PayResult
from sentinel.web import users
from sentinel.web.oauth import SentinelOAuthProvider
from sentinel.web.users import Identity

REASON = "Need 2026 email open-rate benchmarks for the Q3 report the owner asked for; DataCo is named in the brief."
USER_ID = "0b6a8f4e-3c1d-4e2b-9a7f-5d6c8e9f0a1b"


class FakeCdp:
    """Stands in for CdpClient: records every (method, account name) call, knows which names exist, can be slow."""

    def __init__(self) -> None:
        self.accounts: set[str] = set()
        self.calls: list[tuple[str, str | None]] = []
        self.opened = self.closed = 0
        self.delay_s = 0.0
        self.evm = self  # the code calls cdp.evm.get_account and cdp.evm.get_or_create_account

    def __call__(self) -> "FakeCdp":
        self.opened += 1
        return self

    async def __aenter__(self) -> "FakeCdp":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed += 1

    async def get_account(self, address: str | None = None, name: str | None = None) -> SimpleNamespace:
        """Same parameters as the real SDK, where a positional argument would be an address, not a name."""
        self.calls.append(("get_account", name))
        await asyncio.sleep(self.delay_s)
        if address is not None or name is None or name not in self.accounts:
            raise LookupError("no such CDP account")  # the real SDK raises an ApiError with http_code 404
        return SimpleNamespace(name=name, address=address_for(name))

    async def get_or_create_account(self, name: str | None = None) -> SimpleNamespace:
        self.calls.append(("get_or_create_account", name))
        await asyncio.sleep(self.delay_s)
        if name is None:
            raise ValueError("an unnamed account could never be found again")
        self.accounts.add(name)
        return SimpleNamespace(name=name, address=address_for(name))


@pytest.fixture
def cdp(monkeypatch):
    """Replace CDP inside wallets.py with a FakeCdp; the real cdp_wallet and open_user_wallet still run."""
    fake = FakeCdp()
    monkeypatch.setattr(wallets, "CdpClient", fake)
    monkeypatch.setattr(wallets, "EvmLocalAccount", lambda server_account: server_account)  # needs a live client
    monkeypatch.delenv(wallets.ENV_VAR, raising=False)
    return fake


# Wallet seam

@pytest.mark.anyio
async def test_a_pay_call_only_looks_up_the_account_named_by_the_users_uuid(cdp):
    user_id = uuid.uuid4()
    cdp.accounts.add(str(user_id))
    async with wallets.open_user_wallet(user_id) as signer:
        assert signer.address == address_for(str(user_id))
        assert cdp.closed == 0  # the CDP client stays open while the wallet is held: every signature is a call
    assert cdp.calls == [("get_account", str(user_id))]
    assert cdp.closed == 1


@pytest.mark.anyio
async def test_a_lookup_never_creates_a_missing_account(cdp):
    user_id = str(uuid.uuid4())
    with pytest.raises(LookupError):
        async with wallets.open_user_wallet(user_id):
            pass
    assert cdp.calls == [("get_account", user_id)]
    assert user_id not in cdp.accounts


@pytest.mark.anyio
async def test_only_create_true_gets_or_creates_the_account(cdp):
    user_id = str(uuid.uuid4())
    async with wallets.open_user_wallet(user_id, create=True) as created:
        pass
    async with wallets.open_user_wallet(user_id) as found:
        pass
    assert created.address == found.address == address_for(user_id)
    assert cdp.calls == [("get_or_create_account", user_id), ("get_account", user_id)]


@pytest.mark.anyio
async def test_a_uuid_and_its_canonical_string_open_the_same_account_and_it_is_never_sentinel(cdp):
    user_id = uuid.uuid4()
    for form in (user_id, str(user_id)):
        async with wallets.open_user_wallet(form, create=True):
            pass
    names = {name for _, name in cdp.calls}
    assert names == {str(user_id)} and "sentinel" not in names
    assert len(str(user_id)) == 36  # CDP account names are 2 to 36 characters


@pytest.mark.parametrize("user_id", [
    "sentinel", "", "research-agent", f"user-{USER_ID}", USER_ID.upper(), USER_ID.replace("-", ""),
    "{" + USER_ID + "}", f"urn:uuid:{USER_ID}", f" {USER_ID}", f"{USER_ID}\n", 42, None,
])
def test_anything_but_a_uuid_is_refused_before_any_cdp_call(cdp, user_id):
    with pytest.raises(ValueError, match="uuid"):
        wallets.open_user_wallet(user_id)
    with pytest.raises(ValueError, match="uuid"):
        wallets.open_user_wallet(user_id, create=True)
    assert cdp.opened == 0 and cdp.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("create", [False, True])
async def test_a_slow_cdp_call_times_out_instead_of_holding_the_payment(cdp, monkeypatch, create):
    assert wallets.CDP_TIMEOUT_S == 15
    monkeypatch.setattr(wallets, "CDP_TIMEOUT_S", 0.05)
    cdp.delay_s = 5.0
    user_id = str(uuid.uuid4())
    cdp.accounts.add(user_id)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async with wallets.open_user_wallet(user_id, create=create):
            pass
    assert time.monotonic() - started < 2
    assert cdp.closed == 1  # the CDP client is closed even though the lookup never finished


@pytest.mark.anyio
async def test_only_the_lookup_is_bounded_not_the_time_a_payment_holds_the_wallet(cdp, monkeypatch):
    monkeypatch.setattr(wallets, "CDP_TIMEOUT_S", 0.05)
    user_id = str(uuid.uuid4())
    cdp.accounts.add(user_id)
    async with wallets.open_user_wallet(user_id) as signer:
        await asyncio.sleep(0.2)  # a payment holds the wallet for its whole exchange with the seller
    assert signer.address == address_for(user_id)


# Books: each user's agent has its own cash account; the legacy agents keep the shared 1010

def new_agent(conn, own_cash=True):
    """Make an agent the way first sign in will and return (name, agent id, cash account id).

    user-<uuid>, the default limits, the shared 6100 expense account and, unless own_cash is False, its own cash
    account 1010-<agent id>.
    """
    name = f"user-{uuid.uuid4()}"
    agent_id = db.insert(
        conn,
        "INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id) VALUES (%s, %s, %s, %s)",
        (name, db.usdc(db.DEFAULT_PER_PAYMENT_CAP), db.usdc(db.DEFAULT_WEEKLY_BUDGET), db.account_id(conn, "6100")),
    )
    cash_id = None
    if own_cash:
        cash_id = db.insert(
            conn,
            "INSERT INTO accounts (code, name, type) VALUES (%s, %s, 'asset')",
            (f"1010-{agent_id}", f"USDC Cash {address_for(name)}"),
        )
        conn.execute("UPDATE agents SET cash_account_id = %s WHERE id = %s", (cash_id, agent_id))
    return name, agent_id, cash_id


def settle(conn, agent, amount=500_000, url="http://x/a"):
    """Approve and settle one DataCo payment for this agent; returns the journal entry id."""
    quote = {"url": url, "pay_to": DATACO, "amount": amount, "asset": ASSET, "network": NETWORK, "timeout_s": 300}
    attempt_id = attempts.record_attempt(conn, agent, url, None, quote, "approved", "ALLOW", "clean")
    return ledger.settle_attempt(conn, attempt_id, f"0xtx{attempt_id}", b"body")


def entry_lines(conn, entry_id):
    """The (account code, debit, credit) lines of one journal entry, in atomic units."""
    rows = conn.execute(
        "SELECT a.code, (l.debit * 1000000)::bigint AS debit, (l.credit * 1000000)::bigint AS credit"
        " FROM journal_lines l JOIN accounts a ON a.id = l.account_id WHERE l.entry_id = %s",
        (entry_id,),
    ).fetchall()
    return {(r["code"], r["debit"], r["credit"]) for r in rows}


def test_legacy_agents_still_credit_the_shared_1010(conn):
    assert entry_lines(conn, settle(conn, "research-agent")) == {("6100", 500_000, 0), ("1010", 0, 500_000)}


def test_an_agent_with_its_own_cash_account_is_credited_there_and_the_books_still_balance(conn):
    name, agent_id, _ = new_agent(conn)
    settle(conn, "research-agent")
    entry = settle(conn, name, amount=1_000_000)
    assert entry_lines(conn, entry) == {("6100", 1_000_000, 0), (f"1010-{agent_id}", 0, 1_000_000)}
    rows = {r["code"]: r for r in ledger.trial_balance(conn)}
    assert rows["1010"]["credit"] == 500_000 and rows[f"1010-{agent_id}"]["credit"] == 1_000_000
    assert rows["6100"]["debit"] == 1_500_000
    assert sum(r["debit"] for r in rows.values()) == sum(r["credit"] for r in rows.values())


def test_the_database_refuses_two_agents_sharing_one_cash_account(conn):
    _, _, cash_id = new_agent(conn)
    _, other, _ = new_agent(conn, own_cash=False)
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        conn.execute("UPDATE agents SET cash_account_id = %s WHERE id = %s", (cash_id, other))


def test_the_database_refuses_two_users_sharing_a_wallet_or_an_agent(conn):
    def add_user(email, agent_id, wallet):
        conn.execute(
            "INSERT INTO users (email, email_key, agent_id, wallet_address) VALUES (%s, %s, %s, %s)",
            (email, email.lower(), agent_id, wallet),
        )

    _, mine, _ = new_agent(conn)
    _, spare, _ = new_agent(conn)
    wallet = address_for("ann")
    add_user("ann@example.com", mine, wallet)
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        add_user("bo@example.com", spare, "0x" + wallet[2:].upper())  # the same wallet, in other letter case
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        add_user("cy@example.com", mine, address_for("cy"))  # the same agent
    add_user("dee@example.com", spare, address_for("dee"))  # its own agent and wallet are fine


# Isolation: the price jump baseline is per agent

def test_one_agents_settled_prices_do_not_move_another_agents_price_baseline(conn):
    other, _, _ = new_agent(conn)
    for i in range(3):
        settle(conn, "research-agent", url=f"http://x/{i}")  # research-agent's baseline for DataCo: $0.50
    surge = Quote("http://x/surge", DATACO, 2_000_000, ASSET, NETWORK, 300)

    assert attempts.load_history(conn, other, asdict(surge), guard.POLICY)["seller_settled"] == ()
    allowed = payments.authorize(conn, other, REASON, surge)  # no baseline of its own, so $2.00 is no jump
    ledger.settle_attempt(conn, allowed, "0xother", b"body")
    with pytest.raises(payments.PaymentRejected) as blocked:
        payments.authorize(conn, "research-agent", REASON, surge)
    assert blocked.value.rule == "price_jump"

    assert attempts.load_history(conn, "research-agent", asdict(surge), guard.POLICY)["seller_settled"] == (
        500_000, 500_000, 500_000,
    )
    assert attempts.load_history(conn, other, asdict(surge), guard.POLICY)["seller_settled"] == (2_000_000,)


# Provisioning through web/users.py, with a fake wallet opener behind the real open_user_wallet

ISSUER = TEST_BASE_URL


def count(conn, table):
    """Rows in a table, so tests can check exactly what a call added."""
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


async def first_sign_in(conn, connect, email):
    """Sign a new user in the way the site does: upsert, then ensure_wallet. Returns (user row, address)."""
    user = users.find_or_create_user(conn, email, email.lower())
    return user, await users.ensure_wallet(connect, user["id"])


@pytest.mark.anyio
async def test_first_sign_in_creates_exactly_one_agent_and_cash_account_with_the_default_limits(
    conn, connect, fake_wallets
):
    agents_before, accounts_before = count(conn, "agents"), count(conn, "accounts")
    user, address = await first_sign_in(conn, connect, "ann@example.com")

    assert user["wallet_address"] is None and user["agent_id"] is None
    assert fake_wallets.opened == [(user["id"], True)]
    assert address == fake_wallets.address_for(user["id"])
    assert count(conn, "agents") == agents_before + 1 and count(conn, "accounts") == accounts_before + 1
    row = conn.execute(
        "SELECT u.wallet_address, u.wallet_created_at, a.id, a.name, (a.per_payment_cap * 1000000)::bigint AS cap,"
        " (a.weekly_budget * 1000000)::bigint AS budget, e.code AS expense, c.code AS cash, c.name AS cash_name,"
        " c.type AS cash_type FROM users u JOIN agents a ON a.id = u.agent_id"
        " JOIN accounts e ON e.id = a.expense_account_id JOIN accounts c ON c.id = a.cash_account_id"
        " WHERE u.id = %s",
        (user["id"],),
    ).fetchone()
    assert row["name"] == f"user-{user['id']}" == users.agent_name(user["id"])
    assert row["wallet_address"] == address and row["wallet_created_at"] is not None
    assert (row["cap"], row["budget"]) == (db.DEFAULT_PER_PAYMENT_CAP, db.DEFAULT_WEEKLY_BUDGET)
    assert row["expense"] == "6100"
    assert (row["cash"], row["cash_name"], row["cash_type"]) == (f"1010-{row['id']}", f"USDC Cash {address}", "asset")
    assert users.identity_for(conn, user["id"]) == Identity(user["id"], row["name"], address)


@pytest.mark.anyio
async def test_ensure_wallet_is_idempotent_and_calls_cdp_only_once(conn, connect, fake_wallets):
    user, address = await first_sign_in(conn, connect, "ann@example.com")
    agents, accounts = count(conn, "agents"), count(conn, "accounts")
    assert await users.ensure_wallet(connect, user["id"]) == address
    assert users.find_or_create_user(conn, "ann@example.com", "ann@example.com")["wallet_address"] == address
    assert await users.ensure_wallet(connect, user["id"]) == address
    assert fake_wallets.opened == [(user["id"], True)]
    assert (count(conn, "agents"), count(conn, "accounts")) == (agents, accounts)


def test_create_books_is_idempotent_and_refuses_a_different_address(conn):
    user = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    address = address_for(user["id"])
    agent_id = users.create_books(conn, user["id"], address)
    agents, accounts = count(conn, "agents"), count(conn, "accounts")
    assert users.create_books(conn, user["id"], address) == agent_id
    assert users.create_books(conn, user["id"], "0x" + address[2:].upper()) == agent_id  # same wallet, other case
    with pytest.raises(RuntimeError, match="different wallet"):
        users.create_books(conn, user["id"], address_for("someone else"))
    assert (count(conn, "agents"), count(conn, "accounts")) == (agents, accounts)
    assert users.load_user(conn, user["id"])["wallet_address"] == address


@pytest.mark.parametrize("address", ["", "0x123", "0x" + "g" * 40, address_for("a") + "0", address_for("a") + "\n",
                                     " " + address_for("a")])
def test_create_books_refuses_a_malformed_address_and_writes_nothing(conn, address):
    user = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    agents = count(conn, "agents")
    with pytest.raises(ValueError):
        users.create_books(conn, user["id"], address)
    assert count(conn, "agents") == agents and users.load_user(conn, user["id"])["agent_id"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("knob, error", [("fail", RuntimeError), ("timeout", TimeoutError)])
async def test_a_failed_or_timed_out_wallet_open_leaves_no_agent_account_or_wallet(
    conn, connect, fake_wallets, knob, error
):
    user = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    agents, accounts = count(conn, "agents"), count(conn, "accounts")
    setattr(fake_wallets, knob, True)
    with pytest.raises(error):
        await users.ensure_wallet(connect, user["id"])
    assert fake_wallets.opened == [(user["id"], True)]
    assert (count(conn, "agents"), count(conn, "accounts")) == (agents, accounts)
    stored = users.load_user(conn, user["id"])
    assert stored["wallet_address"] is None and stored["agent_id"] is None and stored["wallet_created_at"] is None
    assert conn.execute("SELECT 1 FROM agents WHERE name = %s", (users.agent_name(user["id"]),)).fetchone() is None
    assert users.identity_for(conn, user["id"]) is None

    setattr(fake_wallets, knob, False)  # the retry on the account page then succeeds
    assert await users.ensure_wallet(connect, user["id"]) == address_for(user["id"])
    assert count(conn, "agents") == agents + 1


@pytest.mark.anyio
async def test_ensure_wallet_refuses_unknown_and_malformed_users_before_any_cdp_call(conn, connect, fake_wallets):
    with pytest.raises(LookupError):
        await users.ensure_wallet(connect, str(uuid.uuid4()))
    for bad in ("sentinel", USER_ID.upper(), "ann@example.com"):
        with pytest.raises(ValueError, match="uuid"):
            await users.ensure_wallet(connect, bad)
    assert fake_wallets.opened == []


@pytest.mark.anyio
async def test_the_database_refuses_a_second_user_with_the_same_wallet_in_any_letter_case(
    conn, connect, fake_wallets
):
    ann, address = await first_sign_in(conn, connect, "ann@example.com")
    bo = users.find_or_create_user(conn, "bo@example.com", "bo@example.com")
    agents, accounts = count(conn, "agents"), count(conn, "accounts")
    for shared in (address, "0x" + address[2:].upper()):
        with pytest.raises(db.UniqueViolation):
            users.create_books(conn, bo["id"], shared)  # its own savepoint rolls back the agent and account
    assert (count(conn, "agents"), count(conn, "accounts")) == (agents, accounts)
    assert users.load_user(conn, bo["id"])["wallet_address"] is None


def test_the_database_refuses_users_sharing_an_agent_and_agents_sharing_a_cash_account(conn):
    ann = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    bo = users.find_or_create_user(conn, "bo@example.com", "bo@example.com")
    ann_agent = users.create_books(conn, ann["id"], address_for(ann["id"]))
    bo_agent = users.create_books(conn, bo["id"], address_for(bo["id"]))
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        conn.execute("UPDATE users SET agent_id = %s WHERE id = %s", (ann_agent, bo["id"]))
    with pytest.raises(db.UniqueViolation), db.transaction(conn):
        conn.execute(
            "UPDATE agents SET cash_account_id = (SELECT cash_account_id FROM agents WHERE id = %s) WHERE id = %s",
            (ann_agent, bo_agent),
        )
    with pytest.raises(db.IntegrityError), db.transaction(conn):
        conn.execute("UPDATE users SET wallet_address = NULL WHERE id = %s", (ann["id"],))  # both or neither


# Isolation between two signed in users

@pytest.mark.anyio
async def test_two_users_get_their_own_agents_signers_and_cash_accounts(conn, connect, fake_wallets):
    ann, ann_address = await first_sign_in(conn, connect, "ann@example.com")
    bo, bo_address = await first_sign_in(conn, connect, "bo@example.com")
    ann_id, bo_id = users.identity_for(conn, ann["id"]), users.identity_for(conn, bo["id"])
    assert ann_id is not None and bo_id is not None
    assert ann_id.agent != bo_id.agent and ann_address != bo_address

    async with wallets.open_user_wallet(ann["id"]) as ann_signer, wallets.open_user_wallet(bo["id"]) as bo_signer:
        assert (ann_signer.address, bo_signer.address) == (ann_address, bo_address)
    assert fake_wallets.opened[-2:] == [(ann["id"], False), (bo["id"], False)]

    def pay(agent, amount, path):
        quote = Quote(f"http://seller.test/{path}", DATACO, amount, ASSET, NETWORK, 300)
        attempt_id = payments.authorize(conn, agent, REASON, quote)
        return ledger.settle_attempt(conn, attempt_id, f"0xtx{attempt_id}", b"body")

    ann_cash, bo_cash = (
        conn.execute(
            "SELECT c.code FROM agents a JOIN accounts c ON c.id = a.cash_account_id WHERE a.name = %s", (agent,)
        ).fetchone()["code"]
        for agent in (ann_id.agent, bo_id.agent)
    )
    assert ann_cash != bo_cash
    assert entry_lines(conn, pay(ann_id.agent, 500_000, "a")) == {("6100", 500_000, 0), (ann_cash, 0, 500_000)}
    assert entry_lines(conn, pay(bo_id.agent, 1_000_000, "b")) == {("6100", 1_000_000, 0), (bo_cash, 0, 1_000_000)}
    pay(ann_id.agent, 250_000, "c")

    assert users.account_view(conn, ann["id"])["week_spend"] == 750_000
    assert users.account_view(conn, bo["id"])["week_spend"] == 1_000_000
    balances = {r["code"]: r for r in ledger.trial_balance(conn)}
    assert balances[ann_cash]["credit"] == 750_000 and balances[bo_cash]["credit"] == 1_000_000


# Identity claims

def test_identity_claims_round_trip():
    identity = Identity(USER_ID, f"user-{USER_ID}", address_for(USER_ID))
    claims = identity.claims(ISSUER)
    assert claims == {"iss": ISSUER, "sub": USER_ID, "agent": f"user-{USER_ID}", "wallet": address_for(USER_ID)}
    assert Identity.from_claims(claims) == identity
    mixed_case = Identity(USER_ID, f"user-{USER_ID}", "0x" + address_for(USER_ID)[2:].upper())
    assert Identity.from_claims(mixed_case.claims(ISSUER)) == mixed_case  # the address comes back as stored


GOOD_CLAIMS = {"iss": ISSUER, "sub": USER_ID, "agent": f"user-{USER_ID}", "wallet": address_for(USER_ID)}


@pytest.mark.parametrize("claims", [
    None,
    {},
    ["sub"],
    "sub",
    {k: v for k, v in GOOD_CLAIMS.items() if k != "sub"},
    {k: v for k, v in GOOD_CLAIMS.items() if k != "agent"},
    {k: v for k, v in GOOD_CLAIMS.items() if k != "wallet"},
    {**GOOD_CLAIMS, "sub": USER_ID.upper(), "agent": f"user-{USER_ID.upper()}"},
    {**GOOD_CLAIMS, "sub": "research-agent", "agent": "user-research-agent"},
    {**GOOD_CLAIMS, "sub": 42},
    {**GOOD_CLAIMS, "sub": ""},
    {**GOOD_CLAIMS, "agent": "research-agent"},
    {**GOOD_CLAIMS, "agent": f"user-{uuid.uuid4()}"},
    {**GOOD_CLAIMS, "agent": None},
    {**GOOD_CLAIMS, "wallet": "0x123"},
    {**GOOD_CLAIMS, "wallet": address_for(USER_ID) + "\n"},
    {**GOOD_CLAIMS, "wallet": None},
    {**GOOD_CLAIMS, "wallet": 0},
])
def test_from_claims_refuses_a_missing_or_malformed_sub_agent_or_wallet(claims):
    with pytest.raises(ValueError):
        Identity.from_claims(claims)


def test_agent_name_needs_a_canonical_user_id():
    assert users.agent_name(USER_ID) == f"user-{USER_ID}"
    for bad in (USER_ID.upper(), "sentinel", ""):
        with pytest.raises(ValueError):
            users.agent_name(bad)


# Users, sessions and sign out everywhere

def test_find_or_create_user_is_case_insensitive_and_mail_goes_to_the_address_on_file(conn):
    first = users.find_or_create_user(conn, "Ann@Example.com", "ann@example.com")
    again = users.find_or_create_user(conn, "ann@EXAMPLE.com", "ANN@Example.COM")
    assert again["id"] == first["id"] and str(uuid.UUID(first["id"])) == first["id"]
    assert again["email"] == "Ann@Example.com" and again["email_key"] == "ann@example.com"
    assert users.load_user(conn, first["id"])["last_sign_in_at"] is not None
    assert users.stored_email(conn, "ann@example.com") == "Ann@Example.com"
    assert users.stored_email(conn, "ANN@example.com") == "Ann@Example.com"
    assert users.stored_email(conn, "bo@example.com") is None
    assert count(conn, "users WHERE email_key = 'ann@example.com'") == 1


def test_load_user_and_identity_for_refuse_unknown_and_malformed_ids(conn):
    user = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    assert users.load_user(conn, user["id"])["email"] == "ann@example.com"
    assert users.identity_for(conn, user["id"]) is None  # no wallet yet
    for bad in (str(uuid.uuid4()), user["id"].upper(), "not a uuid", ""):
        assert users.load_user(conn, bad) is None
        assert users.identity_for(conn, bad) is None
        assert users.session_user(conn, bad, 0) is None


def test_session_user_honours_the_epoch(conn):
    user = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    assert user["session_epoch"] == 0
    assert users.session_user(conn, user["id"], 0)["id"] == user["id"]
    assert users.session_user(conn, user["id"], 1) is None
    users.sign_out_everywhere(conn, user["id"])
    assert users.session_user(conn, user["id"], 0) is None
    assert users.session_user(conn, user["id"], 1)["session_epoch"] == 1


def add_grant(conn, user_id, nonce, revoked=False):
    """Insert one approved consent (grant) for the user under a test client; returns its id."""
    conn.execute(
        "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
        " VALUES ('test-client', '{}', 'none', 'ip') ON CONFLICT DO NOTHING"
    )
    return db.insert(
        conn,
        "INSERT INTO oauth_codes (code_hash, request_nonce, client_id, user_id, redirect_uri,"
        " redirect_uri_provided_explicitly, code_challenge, scopes, resource, expires_at, grant_expires_at, revoked_at)"
        " VALUES (%s, %s, 'test-client', %s, 'https://claude.ai/api/mcp/auth_callback', true, 'challenge', '{pay}',"
        " %s, now() + interval '5 minutes', now() + interval '90 days',"
        " CASE WHEN %s THEN timestamptz '2026-01-01 00:00:00+00' END)",
        (f"hash-{nonce}", nonce, user_id, ISSUER + "/mcp", revoked),
    )


def test_sign_out_everywhere_revokes_every_live_grant_of_that_user_only_and_bumps_the_epoch(conn):
    ann = users.find_or_create_user(conn, "ann@example.com", "ann@example.com")
    bo = users.find_or_create_user(conn, "bo@example.com", "bo@example.com")
    ann_live = [add_grant(conn, ann["id"], f"ann-{i}") for i in range(2)]
    ann_old = add_grant(conn, ann["id"], "ann-old", revoked=True)
    bo_live = add_grant(conn, bo["id"], "bo-0")

    users.sign_out_everywhere(conn, ann["id"])

    revoked = {
        r["id"]: r["revoked_at"]
        for r in conn.execute("SELECT id, revoked_at FROM oauth_codes WHERE id = ANY(%s)",
                              ([*ann_live, ann_old, bo_live],)).fetchall()
    }
    assert all(revoked[g] is not None for g in ann_live)
    assert revoked[ann_old].year == 2026 and revoked[ann_old].month == 1  # an earlier revocation keeps its time
    assert revoked[bo_live] is None
    assert users.load_user(conn, ann["id"])["session_epoch"] == 1
    assert users.load_user(conn, bo["id"])["session_epoch"] == 0
    with pytest.raises(LookupError):
        users.sign_out_everywhere(conn, str(uuid.uuid4()))


@pytest.mark.anyio
async def test_account_view_shows_the_email_wallet_agent_limits_and_week_spend(conn, connect, fake_wallets):
    user = users.find_or_create_user(conn, "Ann@Example.com", "ann@example.com")
    assert users.account_view(conn, user["id"]) == {
        "email": "Ann@Example.com", "wallet_address": None, "agent": None, "agent_id": None,
        "per_payment_cap": None, "weekly_budget": None, "week_spend": 0,
    }
    address = await users.ensure_wallet(connect, user["id"])
    agent_id = conn.execute("SELECT agent_id FROM users WHERE id = %s", (user["id"],)).fetchone()["agent_id"]
    assert users.account_view(conn, user["id"]) == {
        "email": "Ann@Example.com", "wallet_address": address, "agent": f"user-{user['id']}", "agent_id": agent_id,
        "per_payment_cap": db.DEFAULT_PER_PAYMENT_CAP, "weekly_budget": db.DEFAULT_WEEKLY_BUDGET, "week_spend": 0,
    }
    with pytest.raises(LookupError):
        users.account_view(conn, str(uuid.uuid4()))


# Over HTTP: the whole app, users signed in through the site, real OAuth tokens on POST /mcp

ANN_IP, BO_IP = "203.0.113.10", "203.0.113.20"


async def connected(browser, mailer, email, ip):
    """A browser at this IP that signed in as email and connected an app; returns (client, ConnectResult)."""
    client = browser(ip)
    return client, await connect_flow(client, mailer, email)


def pay_args(path, reason=REASON):
    """The pay arguments for one URL on the fake seller host."""
    return {"url": f"http://seller.test/{path}", "reason": reason}


def cash_code(conn, user_id):
    """The code of the cash account the user's agent is credited from."""
    return conn.execute(
        "SELECT c.code FROM users u JOIN agents a ON a.id = u.agent_id JOIN accounts c ON c.id = a.cash_account_id"
        " WHERE u.id = %s",
        (user_id,),
    ).fetchone()["code"]


def entry_for(conn, request_id):
    """The journal entry id booked for one payment request."""
    return conn.execute("SELECT id FROM journal_entries WHERE attempt_id = %s", (request_id,)).fetchone()["id"]


@pytest.mark.anyio
async def test_a_token_resolves_to_its_own_users_agent_and_wallet(
    conn, connect, settings, browser, mailer, fake_wallets, pay_spy
):
    client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    assert users.canonical_id(ann.user_id) == ann.user_id
    expected = Identity(ann.user_id, users.agent_name(ann.user_id), fake_wallets.address_for(ann.user_id))
    assert users.identity_for(conn, ann.user_id) == expected

    access = await SentinelOAuthProvider(settings, connect).load_access_token(ann.access_token)
    assert access is not None and access.subject == ann.user_id
    assert Identity.from_claims(access.claims) == expected

    r = await call_tool(client, ann.access_token, "pay", pay_args("a"))
    assert r["status"] == "SETTLED"
    [(_, signer, agent, reason)] = pay_spy.calls
    assert (agent, signer.address, reason) == (expected.agent, expected.wallet, REASON)


@pytest.mark.anyio
async def test_two_users_signed_in_through_the_site_pay_from_their_own_signers_into_their_own_cash_accounts(
    conn, browser, mailer, fake_wallets, pay_spy
):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    assert ann.user_id != bo.user_id

    a = await call_tool(ann_client, ann.access_token, "pay", pay_args("a"))
    pay_spy.amount = 1_000_000
    b = await call_tool(bo_client, bo.access_token, "pay", pay_args("b"))
    assert a["status"] == b["status"] == "SETTLED"

    assert [call[2] for call in pay_spy.calls] == [users.agent_name(ann.user_id), users.agent_name(bo.user_id)]
    assert [call[1].address for call in pay_spy.calls] == [
        fake_wallets.address_for(ann.user_id), fake_wallets.address_for(bo.user_id),
    ]
    ann_cash, bo_cash = cash_code(conn, ann.user_id), cash_code(conn, bo.user_id)
    assert ann_cash != bo_cash and "1010" not in (ann_cash, bo_cash)
    for user, cash in ((ann, ann_cash), (bo, bo_cash)):
        assert cash == f"1010-{users.load_user(conn, user.user_id)['agent_id']}"
    assert entry_lines(conn, entry_for(conn, a["request_id"])) == {("6100", 500_000, 0), (ann_cash, 0, 500_000)}
    assert entry_lines(conn, entry_for(conn, b["request_id"])) == {("6100", 1_000_000, 0), (bo_cash, 0, 1_000_000)}


@pytest.mark.anyio
async def test_users_cannot_read_each_others_requests_over_http(browser, mailer, fake_wallets, pay_spy):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    paid = await call_tool(ann_client, ann.access_token, "pay", pay_args("a"))
    assert paid["status"] == "SETTLED"

    theirs = await call_tool(bo_client, bo.access_token, "payment_status", {"request_id": paid["request_id"]})
    assert theirs["status"] == "NOT_FOUND" and theirs["body"] is None and theirs["tx_hash"] is None
    mine = await call_tool(ann_client, ann.access_token, "payment_status", {"request_id": paid["request_id"]})
    assert mine["status"] == "SETTLED" and mine["body"] == paid["body"]


@pytest.mark.anyio
async def test_users_do_not_share_a_budget_over_http(conn, browser, mailer, fake_wallets, pay_spy):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    conn.execute(
        "UPDATE agents SET weekly_budget = %s WHERE name = %s", (db.usdc(750_000), users.agent_name(ann.user_id))
    )
    assert (await call_tool(ann_client, ann.access_token, "pay", pay_args("a")))["status"] == "SETTLED"
    over = await call_tool(ann_client, ann.access_token, "pay", pay_args("b"))
    assert over["status"] == "BLOCKED" and over["rule"] == "budget"
    for path in ("a", "b"):  # Bo's budget is untouched by Ann's spend
        assert (await call_tool(bo_client, bo.access_token, "pay", pay_args(path)))["status"] == "SETTLED"
    assert users.account_view(conn, ann.user_id)["week_spend"] == 500_000
    assert users.account_view(conn, bo.user_id)["week_spend"] == 1_000_000


@pytest.mark.anyio
async def test_users_cannot_move_each_others_price_baseline_over_http(browser, mailer, fake_wallets, pay_spy):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    for path in ("a", "b", "c"):  # Ann's baseline for DataCo: $0.50
        assert (await call_tool(ann_client, ann.access_token, "pay", pay_args(path)))["status"] == "SETTLED"

    pay_spy.amount = 2_000_000
    for path in ("d", "e"):  # Bo has no baseline of his own, so $2.00 is no jump for him, then becomes his baseline
        assert (await call_tool(bo_client, bo.access_token, "pay", pay_args(path)))["status"] == "SETTLED"
    surge = await call_tool(ann_client, ann.access_token, "pay", pay_args("f"))
    assert surge["status"] == "BLOCKED" and surge["rule"] == "price_jump"


def count_attempts(conn):
    """Payment attempts recorded so far."""
    return count(conn, "payment_attempts")


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "malformed", "unknown", "expired", "revoked"])
async def test_a_missing_invalid_expired_or_revoked_token_gets_401_and_reaches_no_wallet(
    conn, browser, mailer, fake_wallets, pay_spy, kind
):
    client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    opened, attempts_before = list(fake_wallets.opened), count_attempts(conn)
    token = ann.access_token
    if kind == "missing":
        token = None
    elif kind == "malformed":
        token = "not a token"
    elif kind == "unknown":
        token = "sat_" + secrets.token_urlsafe(32)  # well formed, but never issued
    elif kind == "expired":
        conn.execute(
            "UPDATE oauth_tokens SET expires_at = now() - interval '1 second' WHERE kind = 'access'"
            " AND grant_id IN (SELECT id FROM oauth_codes WHERE user_id = %s)",
            (ann.user_id,),
        )
    else:
        users.sign_out_everywhere(conn, ann.user_id)  # revokes every grant of the user

    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    for tool, arguments in (("pay", pay_args("a")), ("payment_status", {"request_id": 1})):
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            headers=headers,
        )
        assert response.status_code == 401, response.text
        assert response.headers["www-authenticate"].lower().startswith("bearer")
    assert fake_wallets.opened == opened and pay_spy.calls == [] and count_attempts(conn) == attempts_before


@pytest.mark.anyio
@pytest.mark.parametrize("knob, message", [
    ("mismatch", "did not match"), ("missing", "could not open your wallet"), ("fail", "could not open your wallet"),
])
async def test_a_wallet_that_does_not_match_or_cannot_be_found_fails_closed_with_no_attempt(
    conn, browser, mailer, fake_wallets, pay_spy, knob, message
):
    client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    attempts_before = count_attempts(conn)
    setattr(fake_wallets, knob, True)
    r = await call_tool(client, ann.access_token, "pay", pay_args("a"))
    assert r["status"] == "FAILED" and message in r["message"] and "nothing was paid" in r["message"]
    assert r["request_id"] is None
    assert pay_spy.calls == [] and count_attempts(conn) == attempts_before
    assert fake_wallets.opened[-1] == (ann.user_id, False)  # a missing wallet is looked up, never created


@pytest.mark.anyio
async def test_paying_never_creates_a_wallet(browser, mailer, fake_wallets, pay_spy):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    signed_in = list(fake_wallets.opened)
    assert signed_in == [(ann.user_id, True), (bo.user_id, True)]  # only first sign in creates

    for path in ("a", "b"):
        await call_tool(ann_client, ann.access_token, "pay", pay_args(path))
        await call_tool(bo_client, bo.access_token, "pay", pay_args(path))
    fake_wallets.missing = True
    await call_tool(ann_client, ann.access_token, "pay", pay_args("c"))

    paid = fake_wallets.opened[len(signed_in):]
    assert len(paid) == 5 and all(create is False for _, create in paid)
    assert {name for name, _ in paid} == {ann.user_id, bo.user_id}


@pytest.mark.anyio
async def test_the_per_user_in_flight_limit_holds_over_http(browser, mailer, fake_wallets, pay_spy, monkeypatch):
    ann_client, ann = await connected(browser, mailer, "ann@example.com", ANN_IP)
    bo_client, bo = await connected(browser, mailer, "bo@example.com", BO_IP)
    release = asyncio.Event()
    entered = []

    async def blocking(self, url, signer, *, agent, reason=None):  # no database work while calls overlap
        entered.append(agent)
        await release.wait()
        return PayResult(200, "ok")

    monkeypatch.setattr(PaySpy, "__call__", blocking)

    async def wait_for(n):
        for _ in range(500):
            if len(entered) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {len(entered)} payments started, expected {n}")

    limit = mcp_server.MAX_IN_FLIGHT_PER_USER
    running = [
        asyncio.create_task(call_tool(ann_client, ann.access_token, "pay", pay_args(f"a{n}"))) for n in range(limit)
    ]
    try:
        await wait_for(limit)
        opened = len(fake_wallets.opened)
        refused = await call_tool(ann_client, ann.access_token, "pay", pay_args("a-extra"))
        assert refused["status"] == "FAILED" and refused["message"] == mcp_server.BUSY_USER
        assert len(fake_wallets.opened) == opened  # the refused call opened no wallet

        running.append(asyncio.create_task(call_tool(bo_client, bo.access_token, "pay", pay_args("b"))))
        await wait_for(limit + 1)  # Bo is not held back by Ann's payments
        assert entered.count(users.agent_name(bo.user_id)) == 1
    finally:
        release.set()
        done = await asyncio.gather(*running)
    assert [d["status"] for d in done] == ["FREE"] * (limit + 1)
    again = await call_tool(ann_client, ann.access_token, "pay", pay_args("a-again"))
    assert again["status"] == "FREE"  # the slots were given back
