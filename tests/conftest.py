"""Shared pytest fixtures. Database tests run in the "test" schema of the Supabase database.

The schema is built once per session from migrations/*.sql (never dropped). Each test then runs
inside a transaction that is rolled back at the end, so tests cannot see each other's rows and the
ledger tables (which reject deletes by design) are never left dirty. Needs DATABASE_URL in .env.

The rest serves the web, OAuth and MCP tests: dev mode settings with test secrets, a recording mailer, a fake wallet
registered as wallets.WALLETS["fake"] (so the real open_user_wallet runs in front of it), a pay function that runs the
real guard and ledger on the test connection, the whole app (web/app.py) under its lifespan, and a browser factory
with one cookie jar and one client IP per simulated user. Plain helper functions (pkce, sign_in, register,
connect_flow, call_tool, provision_user, access_token_for) are imported by the tests like ASSET.
"""
import hashlib
import os
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dotenv import load_dotenv
from fastapi import FastAPI
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from psycopg.types.json import Jsonb

from sentinel import ENV_FILE, db, ledger, payments, wallets
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


@pytest.fixture
def fake_wallets(monkeypatch: pytest.MonkeyPatch) -> FakeWallets:
    """Register FakeWallets as the selected wallet, so wallets.open_user_wallet runs for real in front of it."""
    fake = FakeWallets()
    monkeypatch.setitem(wallets.WALLETS, "fake", (fake.open, ()))
    monkeypatch.setenv(wallets.ENV_VAR, "fake")
    return fake


class PaySpy:
    """A pay function with the real guard and ledger on the test connection but no network: authorize, then settle.

    calls records (url, signer, agent, reason). Knobs: amount (atomic units), pay_to and body set the quote and
    what the seller returns.
    """

    def __init__(self, conn: db.Connection) -> None:
        """Pay DataCo $0.50 for a small JSON body unless the test changes the knobs."""
        self.conn = conn
        self.calls: list[tuple[str, Any, str, str | None]] = []
        self.amount = 500_000
        self.pay_to = DATACO
        self.body = PAID_BODY

    async def __call__(self, url: str, signer: Any, *, agent: str, reason: str | None = None) -> PayResult:
        """Same signature as payments.pay_for."""
        self.calls.append((url, signer, agent, reason))
        quote = Quote(url, self.pay_to, self.amount, ASSET, NETWORK, 300)
        attempt_id = payments.authorize(self.conn, agent, reason, quote)
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
    pay_spy: PaySpy,
) -> AsyncIterator[FastAPI]:
    """The whole app (site, OAuth, /mcp) on the test connection, with its lifespan running."""
    from sentinel.web.app import create_app

    application = create_app(settings, connect=connect, mailer=mailer, pay_fn=pay_spy)

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
    result. A tool error (no structured content) returns the raw result: {"isError": True, "content": [...]}."""
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    result = body["result"]
    structured = result.get("structuredContent")
    return dict(structured) if structured is not None else dict(result)


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
