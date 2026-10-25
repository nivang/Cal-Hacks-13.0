"""Shared pytest fixtures. Database tests run in the "test" schema of the Supabase database.

The schema is built once per session from migrations/*.sql (never dropped). Each test then runs
inside a transaction that is rolled back at the end, so tests cannot see each other's rows and the
ledger tables (which reject deletes by design) are never left dirty. Needs DATABASE_URL in .env.

The rest serves the web, OAuth and MCP tests: dev mode settings with test secrets, a recording mailer, a fake wallet
registered as wallets.WALLETS["fake"] (so the real open_user_wallet runs in front of it), a fake judge (no model
calls), a pay function that runs the real guard, intent review and ledger on the test connection, the whole app
(web/app.py) under its lifespan, and a browser factory with one cookie jar and one client IP per simulated user.
call_tool starts a task first when a pay call names none, as an agent does. Plain helper functions (pkce, sign_in,
register, connect_flow, call_tool, provision_user, access_token_for) are imported by the tests like ASSET.
"""
import hashlib
import json
import os
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dotenv import load_dotenv
from fastapi import FastAPI
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from psycopg.types.json import Jsonb

from sentinel import ENV_FILE, chain, db, judge, ledger, payments, wallets
from sentinel.guard import Quote
from sentinel.payments import PayResult
from sentinel.web import Settings, auth, load_settings, oauth, users
from sentinel.web.mailer import MailError, Message

load_dotenv(ENV_FILE)

TEST_SCHEMA = "test"
NETWORK = "eip155:84532"
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DATACO = "0x290E84b17Ce32cF9403f5D0218cB50ccdcc94781"
SCAMMER = "0xSca000000000000000000000000000000000dead"
TEST_BASE_URL = "http://localhost:8000"  # dev mode, bind host 127.0.0.1
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
BROWSER_IP = "203.0.113.10"
PAID_BODY = b'{"vendor":"DataCo","open_rate":0.214}'
TASK = "Write the Q3 email marketing report; buy the 2026 email open-rate benchmarks from DataCo."


@pytest.fixture(scope="session")
def test_schema() -> Iterator[str]:
    """Create the "test" schema, its tables and seed rows if they do not exist yet, then hand out its name."""
    if not os.environ.get("DATABASE_URL"):
        pytest.fail("DATABASE_URL is not set; database tests need the Supabase connection string in .env")
    with db.connection() as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        with db.transaction(c):
            db.use_schema(c, TEST_SCHEMA)
            db.apply_migrations(c)
            db.init(c)
            db.seed_sellers(
                c, [{"name": "DataCo", "pay_to": DATACO, "network": NETWORK, "asset": ASSET, "status": "approved"}]
            )
    yield TEST_SCHEMA
    db.close_pool()


@pytest.fixture
def conn(test_schema: str) -> Iterator[db.Connection]:
    """A connection inside a transaction that is rolled back after the test, pointed at the test schema."""
    with db.connection() as c, db.transaction(c, rollback=True):
        db.use_schema(c, test_schema)
        yield c


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Run async tests on asyncio only (the trio backend is not installed)."""
    return str(request.param)


# Web, OAuth and MCP fixtures


@pytest.fixture(autouse=True)
def allow_local_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test seller hosts do not resolve, so skip the public address check (tests about it delenv this)."""
    monkeypatch.setenv("SENTINEL_ALLOW_LOCAL_URLS", "1")


class FakeChain:
    """Stands in for chain.wallet_history and chain.wallet_profile: every wallet has a long history unless the test
    marks it unused, new or flagged (lower case addresses), or the chain down."""

    def __init__(self) -> None:
        self.unused: set[str] = set()
        self.new: set[str] = set()
        self.flagged: set[str] = set()
        self.down = False
        self.asked: list[str] = []

    async def profile(self, address: str) -> chain.WalletProfile:
        self.asked.append(address)
        if self.down:
            raise chain.ChainError("explorer and RPC down")
        key = address.lower()
        if key in self.unused:
            return chain.WalletProfile(0, 0, None, False, "explorer")
        first_seen = datetime.now(timezone.utc) - timedelta(hours=2) if key in self.new else None
        return chain.WalletProfile(5, 120, first_seen, key in self.flagged, "explorer")

    async def history(self, address: str) -> chain.WalletHistory:
        self.asked.append(address)
        if self.down:
            raise chain.ChainError("RPC down")
        if address.lower() in self.unused:
            return chain.WalletHistory(0, 0, 0)
        return chain.WalletHistory(tx_count=3, eth=10**15, usdc=1_000_000)


@pytest.fixture(autouse=True)
def fake_chain(monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    """No test reads Base Sepolia: wallet_history and wallet_profile answer from FakeChain."""
    fake = FakeChain()
    monkeypatch.setattr(chain, "wallet_history", fake.history)
    monkeypatch.setattr(chain, "wallet_profile", fake.profile)
    return fake


@pytest.fixture
def settings() -> Settings:
    """Dev mode settings with test secrets (never real ones): base URL http://localhost:8000, bind host 127.0.0.1."""
    env = {
        "DATABASE_URL": "postgresql://test",
        "PUBLIC_BASE_URL": TEST_BASE_URL,
        "SESSION_SECRET": "s" * 40,
        "TOKEN_HASH_SECRET": "t" * 40,
    }
    return load_settings(env, "127.0.0.1")


@pytest.fixture
def connect(conn: db.Connection) -> Callable[[], Any]:
    """A Connect callable that hands out the test's rolled back connection."""

    @contextmanager
    def same_connection() -> Iterator[db.Connection]:
        yield conn

    return same_connection


class RecordingMailer:
    """A Mailer that keeps every message instead of sending it. fail=True makes send raise MailError."""

    def __init__(self) -> None:
        """No messages yet; sending works."""
        self.messages: list[Message] = []
        self.fail = False

    async def send(self, message: Message) -> None:
        """Record the message, or fail the way Resend would."""
        if self.fail:
            raise MailError("Resend answered 500")
        self.messages.append(message)

    def last_token(self) -> str:
        """The magic link token in the last message sent."""
        assert self.messages, "no sign in mail was sent"
        found = re.search(r"/auth/verify\?token=([A-Za-z0-9_-]{43})", self.messages[-1].text)
        assert found, "the last mail holds no sign in link"
        return found.group(1)


@pytest.fixture
def mailer() -> RecordingMailer:
    """A fresh recording mailer."""
    return RecordingMailer()


def fake_wallet_address(name: str) -> str:
    """The address FakeWallets gives the account with this name: stable, so tests can predict it."""
    return "0x" + hashlib.sha256(name.encode()).hexdigest()[:40]


class FakeWallets:
    """A wallet opener registered as wallets.WALLETS["fake"]: records (name, create) and derives the address.

    Knobs: fail (CDP answers with an error), timeout (CDP is too slow), mismatch (CDP hands back some other address)
    and missing (a lookup finds no account; create=True still makes one).
    """

    def __init__(self) -> None:
        """Nothing opened yet; every knob off."""
        self.opened: list[tuple[str, bool]] = []
        self.fail = self.timeout = self.mismatch = self.missing = False

    @staticmethod
    def address_for(name: str) -> str:
        """The address the fake gives the account with this name."""
        return fake_wallet_address(name)

    @asynccontextmanager
    async def open(self, name: str, create: bool) -> AsyncIterator[SimpleNamespace]:
        """The opener: same (account name, create) shape as wallets.cdp_wallet."""
        self.opened.append((name, create))
        if self.fail:
            raise RuntimeError("CDP answered 500")
        if self.timeout:
            raise TimeoutError
        if self.missing and not create:
            raise LookupError("no such CDP account")
        yield SimpleNamespace(address=fake_wallet_address("other " + name if self.mismatch else name))


class FakeJudge:
    """A Judge with no model behind it. Records every case it was shown.

    Knobs: fit (tier 1 verdict, or a dict of URL substring -> verdict, default fits), decision (tier 2 verdict),
    scores (price discovery ratings by offer name substring -> (fit, coverage), default (8, 8)),
    draft (fields to override in the mandate draft), and fail_extract / fail_classify / fail_decide / fail_rate (raise
    JudgeError).
    """

    def __init__(self) -> None:
        """Everything fits; nothing fails."""
        self.fit: str | dict[str, str] = "fits"
        self.decision = "allow"
        self.draft: dict[str, Any] = {}
        self.fail_extract = self.fail_classify = self.fail_decide = self.fail_rate = False
        self.scores: dict[str, tuple[int, int]] = {}
        self.tasks: list[str] = []
        self.classified: list[dict[str, Any]] = []
        self.decided: list[dict[str, Any]] = []
        self.rated: list[dict[str, Any]] = []

    async def extract(self, task: str) -> judge.MandateDraft:
        """A fixed mandate for any task, with the draft knob applied."""
        self.tasks.append(task)
        if self.fail_extract:
            raise judge.JudgeError("extract failed")
        fields: dict[str, Any] = {
            "goal": "Write the Q3 email marketing report.", "expected_types": ["email benchmarks", "market data"],
            "out_of_scope": ["crypto trading signals"], "budget_cents": None, "per_call_cents": None,
        }
        return judge.MandateDraft(**{**fields, **self.draft})

    async def classify(self, case: dict[str, Any]) -> judge.FitOpinion:
        """Tier 1 from the fit knob."""
        self.classified.append(case)
        if self.fail_classify:
            raise judge.JudgeError("classify failed")
        verdict = self.fit
        if isinstance(verdict, dict):
            url = case["payment"]["url"]
            verdict = next((v for k, v in verdict.items() if k in url), "fits")
        return judge.FitOpinion(verdict=verdict, reason=f"Tier 1 says {verdict}.")  # type: ignore[arg-type]

    async def decide(self, case: dict[str, Any]) -> judge.JudgeOpinion:
        """Tier 2 from the decision knob."""
        self.decided.append(case)
        if self.fail_decide:
            raise judge.JudgeError("decide failed")
        return judge.JudgeOpinion(verdict=self.decision, reason=f"The judge says {self.decision}.")  # type: ignore[arg-type]

    async def rate(self, case: dict[str, Any]) -> judge.Ratings:
        """Price discovery ratings from the scores knob (name substring -> (fit, coverage)), default (8, 8)."""
        self.rated.append(case)
        if self.fail_rate:
            raise judge.JudgeError("rate failed")
        sheet = []
        for o in case["offers"]:
            fit, coverage = next((v for k, v in self.scores.items() if k in o["name"]), (8, 8))
            sheet.append(judge.Rating(id=o["id"], fit=fit, coverage=coverage, reason=f"{o['name']} rated {fit}."))
        return judge.Ratings(ratings=sheet)


@pytest.fixture
def fake_judge() -> FakeJudge:
    """A fresh fake judge."""
    return FakeJudge()


@pytest.fixture
def fake_wallets(monkeypatch: pytest.MonkeyPatch) -> FakeWallets:
    """Register FakeWallets as the selected wallet, so wallets.open_user_wallet runs for real in front of it."""
    fake = FakeWallets()
    monkeypatch.setitem(wallets.WALLETS, "fake", (fake.open, ()))
    monkeypatch.setenv(wallets.ENV_VAR, "fake")
    return fake


class PaySpy:
    """A pay function with the real guard, intent review and ledger on the test connection but no network.

    calls records (url, signer, agent, reason); tasks records the task each call ran under. Knobs: amount (atomic
    units), pay_to and body set the quote and what the seller returns.
    """

    def __init__(self, conn: db.Connection) -> None:
        """Pay DataCo $0.50 for a small JSON body unless the test changes the knobs."""
        self.conn = conn
        self.calls: list[tuple[str, Any, str, str | None]] = []
        self.tasks: list[payments.Task | None] = []
        self.amount = 500_000
        self.pay_to = DATACO
        self.body = PAID_BODY

    async def __call__(
        self, url: str, signer: Any, *, agent: str, reason: str | None = None, task: payments.Task | None = None,
        **options: Any,
    ) -> PayResult:
        """Same signature as payments.pay_for."""
        self.calls.append((url, signer, agent, reason))
        self.tasks.append(task)
        quote = Quote(url, self.pay_to, self.amount, ASSET, NETWORK, 300)
        attempt_id = await payments.authorize_task(self.conn, agent, reason, quote, task,
                                                  interactive=options.get("interactive", False),
                                                  approval_id=options.get("approval_id"))
        tx = f"0xtx{attempt_id}"
        ledger.settle_attempt(self.conn, attempt_id, tx, self.body, "application/json")
        return PayResult(200, self.body.decode(), tx, quote, attempt_id, True, "application/json")


@pytest.fixture
def pay_spy(conn: db.Connection) -> PaySpy:
    """A fresh pay spy on the test connection."""
    return PaySpy(conn)


@pytest.fixture
async def app(
    settings: Settings, connect: Callable[[], Any], mailer: RecordingMailer, fake_wallets: FakeWallets,
    pay_spy: PaySpy, fake_judge: FakeJudge,
) -> AsyncIterator[FastAPI]:
    """The whole app (site, OAuth, /mcp) on the test connection, with its lifespan running."""
    from sentinel.web.app import create_app

    application = create_app(settings, connect=connect, mailer=mailer, pay_fn=pay_spy, judge_=fake_judge)

    async def with_lifespan_state(scope: Any, receive: Any, send: Any) -> None:
        """Give the lifespan scope a "state" dict, as uvicorn does (combine_lifespans yields one)."""
        await application({**scope, "state": {}}, receive, send)

    async with run_asgi_lifespan(with_lifespan_state):
        yield application


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """A factory of browsers: each call is a new user agent with its own cookie jar, calling from its own IP.

    Every request carries Origin: TEST_BASE_URL, as a browser posting our own forms would. Redirects are not
    followed, so tests see each step.
    """
    made: list[httpx.AsyncClient] = []

    def make(ip: str = BROWSER_IP) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 50000)), base_url=TEST_BASE_URL,
            follow_redirects=False, headers={"origin": TEST_BASE_URL},
        )
        made.append(client)
        return client

    yield make
    for client in made:
        await client.aclose()


# Helpers (plain functions, imported by tests)


def pkce() -> tuple[str, str]:
    """(code verifier, S256 code challenge)."""
    verifier = secrets.token_urlsafe(32)
    return verifier, auth.b64(hashlib.sha256(verifier.encode()).digest())


def path_of(location: str) -> str:
    """A Location header as a path with its query, for following a redirect on our own origin."""
    parts = urlsplit(location)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def query_of(location: str) -> dict[str, str]:
    """The query parameters of a URL, one value each."""
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def form_value(html: str, name: str) -> str:
    """The value of the first hidden input with this name in a page."""
    found = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', html)
    assert found, f"no {name} field on the page"
    return found.group(1)


async def sign_in(
    client: httpx.AsyncClient, mailer: RecordingMailer, email: str, next: str | None = None
) -> httpx.Response:
    """Sign in through the site: ask for a link, then POST it the way the confirm page does. Returns the 303."""
    data = {"email": email} if next is None else {"email": email, "next": next}
    asked = await client.post("/signin", data=data)
    assert asked.status_code == 200, asked.text
    response = await client.post("/auth/verify", data={"token": mailer.last_token()})
    assert response.status_code == 303, response.text
    return response


async def register(
    client: httpx.AsyncClient, redirect_uris: tuple[str, ...] = (CLAUDE_CALLBACK,),
    auth_method: str = "client_secret_post", **extra: Any,
) -> httpx.Response:
    """POST /register with these redirect URIs and auth method (plus any extra metadata)."""
    body = {"redirect_uris": list(redirect_uris), "token_endpoint_auth_method": auth_method, **extra}
    return await client.post("/register", json=body)


@dataclass(frozen=True)
class ConnectResult:
    """What an AI app holds after connecting: its registration, its tokens, and whose they are."""

    client_id: str
    client_secret: str | None
    access_token: str
    refresh_token: str
    user_id: str
    redirect_uri: str
    code_verifier: str


async def connect_flow(
    client: httpx.AsyncClient, mailer: RecordingMailer, email: str, *, redirect_uri: str = CLAUDE_CALLBACK,
    auth_method: str = "client_secret_post", state: str | None = "state-123", resource: str | None = None,
    client_name: str | None = None,
) -> ConnectResult:
    """Connect an app the way Claude does: register, /authorize, sign in if needed, Allow, then swap the code.

    resource defaults to TEST_BASE_URL + "/mcp". The browser and the app share one client here, which is fine: the
    OAuth endpoints use no cookies.
    """
    extra = {"client_name": client_name} if client_name is not None else {}
    reg = await register(client, (redirect_uri,), auth_method, **extra)
    assert reg.status_code == 201, reg.text
    registration = reg.json()
    verifier, challenge = pkce()
    query = {
        "response_type": "code", "client_id": registration["client_id"], "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "pay",
        "resource": resource or TEST_BASE_URL + "/mcp",
    }
    if state is not None:
        query["state"] = state
    authorized = await client.get("/authorize", params=query)
    assert authorized.status_code == 302, authorized.text
    consent_path = path_of(authorized.headers["location"])
    assert consent_path.startswith("/oauth/consent?req=")

    page = await client.get(consent_path)
    if page.status_code == 303:  # not signed in yet: sign in, which comes back to the consent page
        assert page.headers["location"].startswith("/signin?")
        signed_in = await sign_in(client, mailer, email, next=consent_path)
        assert signed_in.headers["location"] == consent_path
        page = await client.get(consent_path)
    assert page.status_code == 200, page.text

    allowed = await client.post(
        "/oauth/consent",
        data={"req": form_value(page.text, "req"), "csrf": form_value(page.text, "csrf"), "action": "allow"},
    )
    assert allowed.status_code == 303, allowed.text
    location = allowed.headers["location"]
    assert location.startswith(redirect_uri)
    params = query_of(location)
    assert params.get("state") == state and params["iss"] == TEST_BASE_URL

    token_form = {
        "grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri,
        "client_id": registration["client_id"], "code_verifier": verifier,
        "resource": resource or TEST_BASE_URL + "/mcp",
    }
    secret = registration.get("client_secret")
    if auth_method == "client_secret_post" and secret:
        token_form["client_secret"] = secret
    basic = (registration["client_id"], secret) if auth_method == "client_secret_basic" and secret else None
    tokens = await client.post("/token", data=token_form, auth=basic or httpx.USE_CLIENT_DEFAULT)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    cookie = client.cookies.get("sentinel_session") or ""  # "v2.<user id>.<epoch>.<expires>.<nonce>.<signature>"
    return ConnectResult(
        client_id=registration["client_id"], client_secret=secret, access_token=body["access_token"],
        refresh_token=body["refresh_token"], user_id=cookie.split(".")[1] if cookie.count(".") == 5 else "",
        redirect_uri=redirect_uri, code_verifier=verifier,
    )


async def call_tool(
    client: httpx.AsyncClient, access_token: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Call one MCP tool over HTTP (JSON-RPC tools/call to /mcp with the bearer token) and return its structured
    result. A tool error returns the raw result. Payments automatically start a task when omitted."""
    if tool == "pay" and "task_id" not in arguments:
        started = await call_tool(client, access_token, "start_task", {"task": TASK})
        if started.get("task_id"):
            arguments = {"task_id": started["task_id"], **arguments}
    response = await mcp_request(client, access_token, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    result = body["result"]
    structured = result.get("structuredContent")
    return dict(structured) if structured is not None else dict(result)


async def mcp_request(client: httpx.AsyncClient, token: str, payload: dict[str, Any]) -> httpx.Response:
    """A minimal real legacy HTTP MCP client: initialize, notify, then decode the result's SSE event."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    initialized = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
        },
    })
    if initialized.status_code != 200:
        return initialized
    session = initialized.headers.get("mcp-session-id")
    if session:
        headers["mcp-session-id"] = session
    headers["mcp-protocol-version"] = "2025-06-18"
    await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = await client.post("/mcp", headers=headers, json=payload)
    if response.status_code == 200 and "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                body = json.loads(line[6:])
                if body.get("id") == payload.get("id") and ("result" in body or "error" in body):
                    return httpx.Response(200, json=body, request=response.request)
    return response


async def provision_user(connect: Callable[[], Any], email: str) -> users.Identity:
    """A signed up user with a wallet (from the fake_wallets fixture, which must be active) and books."""
    with connect() as conn:
        user = users.find_or_create_user(conn, email, email.lower())
    await users.ensure_wallet(connect, user["id"])
    with connect() as conn:
        identity = users.identity_for(conn, user["id"])
    assert identity is not None
    return identity


def access_token_for(conn: db.Connection, settings: Settings, identity: users.Identity) -> str:
    """A live access token for this user: register a client row, approve a consent for it, mint the tokens."""
    client_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
        " VALUES (%s, %s, 'client_secret_post', 'test')",
        (client_id, Jsonb({"redirect_uris": [CLAUDE_CALLBACK]})),
    )
    pending = oauth.PendingRequest(
        nonce=secrets.token_urlsafe(24), client_id=client_id, redirect_uri=CLAUDE_CALLBACK,
        redirect_uri_provided_explicitly=True, code_challenge=pkce()[1], state=None,
        resource=settings.resource_url, expires=int(oauth.clock()) + oauth.PENDING_TTL_S,
    )
    user = users.load_user(conn, identity.user_id)
    assert user is not None
    assert oauth.approve(conn, settings, pending, identity.user_id, session_epoch=int(user["session_epoch"]))
    grant = conn.execute("SELECT id FROM oauth_codes WHERE request_nonce = %s", (pending.nonce,)).fetchone()
    assert grant is not None
    return oauth.issue_tokens(conn, settings, int(grant["id"])).access_token
