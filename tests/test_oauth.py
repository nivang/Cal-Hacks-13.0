"""Tests for the OAuth authorization server (sentinel/web/oauth.py): registration, consent requests, codes, tokens.

Three levels. Callback level: the provider's nine callbacks and the helpers (approve, pending requests, redirect
rules) called directly. Route level: a tiny Starlette app built from provider.get_routes("/mcp") and driven with
httpx.ASGITransport, so the SDK's own checks (client auth, PKCE, redirect match, expiry) run in front of ours. App
level (the last section): the whole app from conftest (fixtures app and browser), so a browser registers, authorizes,
signs in by magic link, answers the consent page, swaps the code at /token and calls /mcp, including with a real
FastMCP client over StreamingASGITransport.

Everything runs on the rolled back test connection; SQL now() is fixed for the whole test transaction, so windows
and expiries are moved by aging rows with UPDATE, never by sleeping. Callback and route level users are provisioned
with users.find_or_create_user and users.create_books on a fake address; app level users sign in through the site
with the fake wallet from conftest. No CDP, mail or network is touched.
"""
import dataclasses
import json
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

import httpx
import httpx2
import pytest
from conftest import (
    CLAUDE_CALLBACK,
    PAID_BODY,
    TASK,
    TEST_BASE_URL,
    RecordingMailer,
    call_tool,
    fake_wallet_address,
    form_value,
    mcp_request,
    path_of,
    pkce,
    query_of,
    register,
    sign_in,
)
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError
from pydantic import AnyUrl
from starlette.applications import Starlette

from sentinel import db
from sentinel.web import Settings, auth, load_settings, oauth, users
from sentinel.web import ratelimit as rl

RESOURCE = TEST_BASE_URL + "/mcp"
LOOPBACK_CALLBACK = "http://localhost:33418/callback"
UNTRUSTED_CALLBACK = "https://app.example.com/oauth/callback"
IP = "203.0.113.10"
ANTHROPIC_IP = "160.79.104.10"
PAY_URL = "http://seller.test/benchmarks/email"
REASON = "Need 2026 email open-rate benchmarks for the Q3 report the owner asked for; DataCo is named in the brief."


# Fixtures and helpers (settings, connect, pkce, register and query_of come from conftest)


@pytest.fixture
def provider(settings: Settings, connect: Callable[[], Any]) -> oauth.SentinelOAuthProvider:
    """The provider on the test connection."""
    return oauth.SentinelOAuthProvider(settings, connect)


@pytest.fixture
def client_for(provider: oauth.SentinelOAuthProvider) -> Callable[..., httpx.AsyncClient]:
    """A factory of HTTP clients for the provider's routes, each calling from its own IP."""
    app = Starlette(routes=provider.get_routes("/mcp"))

    def make(ip: str = IP) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip, 50000))
        return httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL, follow_redirects=False)

    return make


def new_user(conn: db.Connection, *, wallet: bool = True) -> str:
    """A signed in user (with books on a fake address unless wallet=False). Returns the user id."""
    email = f"oauth-{uuid.uuid4().hex[:12]}@example.com"
    row = users.find_or_create_user(conn, email, email)
    if wallet:
        users.create_books(conn, row["id"], fake_wallet_address(row["id"]))
    return str(row["id"])


@pytest.fixture
def user(conn: db.Connection) -> users.Identity:
    """A provisioned user: wallet, agent "user-<id>" and own cash account."""
    identity = users.identity_for(conn, new_user(conn))
    assert identity is not None
    return identity


def epoch(conn: db.Connection, user_id: str) -> int:
    """The user's current session epoch (what the consent route passes to approve)."""
    row = users.load_user(conn, user_id)
    assert row is not None
    return int(row["session_epoch"])


async def registered(client: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    """A registered client's JSON (fails the test unless 201)."""
    response = await register(client, **kw)
    assert response.status_code == 201, response.text
    return dict(response.json())


def authorize_query(reg: dict[str, Any], challenge: str, **kw: Any) -> dict[str, str]:
    """The /authorize query for a registered client. Pass a value of None to leave a parameter out."""
    query = {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": reg["redirect_uris"][0],
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "state-123", "scope": "pay",
        "resource": RESOURCE,
    }
    query.update(kw)
    return {key: value for key, value in query.items() if value is not None}


def pending_of(settings: Settings, response: httpx.Response) -> oauth.PendingRequest:
    """The pending request a consent redirect carries."""
    location = response.headers["location"]
    assert location.startswith(TEST_BASE_URL + "/oauth/consent?req="), location
    pending = oauth.read_pending(settings, query_of(location)["req"])
    assert pending is not None
    return pending


async def code_for(
    client: httpx.AsyncClient, conn: db.Connection, settings: Settings, user_id: str, **kw: Any
) -> dict[str, Any]:
    """Register, authorize and approve. Returns the client, the PKCE verifier, the code and the redirect."""
    reg = await registered(client, **kw)
    verifier, challenge = pkce()
    response = await client.get("/authorize", params=authorize_query(reg, challenge))
    assert response.status_code == 302, response.text
    location = oauth.approve(
        conn, settings, pending_of(settings, response), user_id, session_epoch=epoch(conn, user_id)
    )
    assert location is not None
    return {"reg": reg, "verifier": verifier, "code": query_of(location)["code"], "location": location}


async def exchange(client: httpx.AsyncClient, ctx: dict[str, Any], **override: str) -> httpx.Response:
    """POST /token for the code in ctx (client_secret_post)."""
    form = {
        "grant_type": "authorization_code", "code": ctx["code"], "redirect_uri": ctx["reg"]["redirect_uris"][0],
        "client_id": ctx["reg"]["client_id"], "client_secret": ctx["reg"].get("client_secret", ""),
        "code_verifier": ctx["verifier"],
    }
    form.update(override)
    return await client.post("/token", data=form)


async def refresh(client: httpx.AsyncClient, ctx: dict[str, Any], refresh_token: str) -> httpx.Response:
    """POST /token with a refresh token for the client in ctx."""
    form = {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": ctx["reg"]["client_id"],
        "client_secret": ctx["reg"]["client_secret"],
    }
    return await client.post("/token", data=form)


async def tokens_for(
    client: httpx.AsyncClient, conn: db.Connection, settings: Settings, user_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The whole callback flow up to the first token pair: (ctx, token response JSON)."""
    ctx = await code_for(client, conn, settings, user_id)
    response = await exchange(client, ctx)
    assert response.status_code == 200, response.text
    return ctx, dict(response.json())


def grant_row(conn: db.Connection, client_id: str) -> dict[str, Any]:
    """The one grant of a client."""
    row = conn.execute("SELECT * FROM oauth_codes WHERE client_id = %s", (client_id,)).fetchone()
    assert row is not None
    return row


def count(conn: db.Connection, table: str) -> int:
    """Rows in a table."""
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def fill_clients(conn: db.Connection, n: int, ip_hash: str | None = None) -> None:
    """Insert n registrations made now, from one IP hash or (None) each from a different one."""
    conn.execute(
        "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
        " SELECT gen_random_uuid()::text, '{}'::jsonb, 'none', coalesce(%s, md5(g::text))"
        " FROM generate_series(1, %s) g",
        (ip_hash, n),
    )


def pending_request(**kw: Any) -> oauth.PendingRequest:
    """A pending request with plausible values."""
    values: dict[str, Any] = {
        "nonce": secrets.token_urlsafe(24), "client_id": str(uuid.uuid4()), "redirect_uri": CLAUDE_CALLBACK,
        "redirect_uri_provided_explicitly": True, "code_challenge": pkce()[1], "state": "s", "resource": RESOURCE,
        "expires": int(oauth.clock()) + oauth.PENDING_TTL_S,
    }
    values.update(kw)
    return oauth.PendingRequest(**values)


# Registration


@pytest.mark.anyio
async def test_registration_derives_the_secret_never_stores_it_and_get_client_recomputes_it(
    client_for, conn, settings, provider
):
    async with client_for() as client:
        reg = await registered(client)
    secret = reg["client_secret"]
    assert secret == oauth.client_secret_for(settings, reg["client_id"])
    assert reg["client_secret_expires_at"] == 0
    row = conn.execute("SELECT to_jsonb(c)::text AS doc FROM oauth_clients c WHERE client_id = %s",
                       (reg["client_id"],)).fetchone()
    assert secret not in row["doc"] and '"client_secret"' not in row["doc"]
    loaded = await provider.get_client(reg["client_id"])
    assert isinstance(loaded, oauth.SentinelClient)
    assert loaded.client_secret == secret and loaded.token_endpoint_auth_method == "client_secret_post"


@pytest.mark.anyio
async def test_a_public_client_gets_no_secret(client_for, provider):
    async with client_for() as client:
        reg = await registered(client, redirect_uris=(LOOPBACK_CALLBACK,), auth_method="none")
    assert "client_secret" not in reg
    loaded = await provider.get_client(reg["client_id"])
    assert loaded is not None and loaded.client_secret is None


@pytest.mark.anyio
async def test_get_client_refuses_unknown_and_malformed_ids(provider):
    assert await provider.get_client(str(uuid.uuid4())) is None
    for bad in ["", "not-a-uuid", "x" * 500, str(uuid.uuid4()).upper(), "1' OR '1'='1"]:
        assert await provider.get_client(bad) is None


@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "vbscript:msgbox",
        "https://user:pw@claude.ai/cb",
        "https://user@claude.ai/cb",
        "https://claude.ai/cb#fragment",
        "http://example.com/cb",
        "https://203.0.113.5/cb",
        "http://10.0.0.1/cb",
        "https://[2001:db8::1]/cb",
        "https://claude.ai/" + "a" * 600,
        "ftp://files.example.com/cb",
    ],
)
@pytest.mark.anyio
async def test_unsafe_redirect_uris_are_refused(client_for, conn, uri):
    async with client_for() as client:
        response = await register(client, redirect_uris=(uri,))
    assert response.status_code == 400, response.text
    assert response.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
    assert count(conn, "oauth_clients") == 0


def test_dot_segments_are_refused_even_before_url_normalization():
    for uri in ["https://claude.ai/a/../cb", "https://claude.ai/a/%2e%2e/cb", "https://claude.ai/./cb",
                "cursor://x/a/%2E%2E/cb"]:
        assert oauth.redirect_uri_problem(uri) is not None, uri
    assert oauth.redirect_uri_problem(CLAUDE_CALLBACK) is None


@pytest.mark.anyio
async def test_dot_segments_never_reach_storage(client_for, conn):
    async with client_for() as client:
        response = await register(client, redirect_uris=("https://claude.ai/a/../b/%2e%2e/cb",))
    if response.status_code == 201:  # pydantic collapsed them first; what is stored has none
        stored = conn.execute("SELECT metadata FROM oauth_clients").fetchone()["metadata"]["redirect_uris"][0]
        assert ".." not in stored and "%2e" not in stored.lower()
    else:
        assert response.status_code == 400


@pytest.mark.parametrize(
    "uri",
    [
        CLAUDE_CALLBACK,
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:6274/oauth/callback",
        "http://127.0.0.1:33418/callback",
        "http://[::1]:8080/callback",
        "cursor://anysphere.cursor-retrieval/oauth/callback",
        "com.example.app:/oauth2redirect",
    ],
)
@pytest.mark.anyio
async def test_safe_redirect_uris_are_accepted(client_for, uri):
    async with client_for() as client:
        assert (await register(client, redirect_uris=(uri,))).status_code == 201


@pytest.mark.anyio
async def test_between_one_and_ten_redirect_uris(client_for):
    eleven = tuple(f"http://localhost:{4000 + n}/cb" for n in range(11))
    async with client_for() as client:
        assert (await register(client, redirect_uris=eleven)).status_code == 400
        assert (await register(client, redirect_uris=())).status_code == 400
        assert (await register(client, redirect_uris=eleven[:10])).status_code == 201


@pytest.mark.parametrize("extra", [{"jwks_uri": "https://app.example.com/jwks.json"}, {"jwks": {"keys": []}}])
@pytest.mark.anyio
async def test_jwks_and_jwks_uri_are_refused(client_for, conn, extra):
    async with client_for() as client:
        response = await register(client, **extra)
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"
    assert count(conn, "oauth_clients") == 0


def test_client_names_are_sanitized():
    assert oauth.sanitize_client_name("  Claude‮​   Code\x00\n ") == "Claude Code"
    assert oauth.sanitize_client_name("Evil App ") == "EvilApp"
    assert oauth.sanitize_client_name("Private") == "Private"
    assert oauth.sanitize_client_name("x" * 300) == "x" * oauth.MAX_CLIENT_NAME
    assert oauth.sanitize_client_name("​\x07 ") is None
    assert oauth.sanitize_client_name(None) is None
    assert oauth.sanitize_client_name("Café ☕") == "Café ☕"


@pytest.mark.anyio
async def test_registration_stores_only_allowlisted_metadata_and_echoes_what_it_stored(client_for, conn):
    extra = {
        "client_name": "My‮App", "logo_uri": "https://app.example.com/logo.png",
        "client_uri": "https://app.example.com", "tos_uri": "https://app.example.com/tos",
        "contacts": ["ops@example.com"], "software_id": "app", "software_version": "1.0",
    }
    async with client_for() as client:
        reg = await registered(client, **extra)
    assert reg["client_name"] == "MyApp"
    assert reg["scope"] == "pay" and reg["grant_types"] == ["authorization_code", "refresh_token"]
    for dropped in ("logo_uri", "client_uri", "tos_uri", "contacts"):
        assert dropped not in reg
    stored = conn.execute("SELECT metadata FROM oauth_clients WHERE client_id = %s",
                          (reg["client_id"],)).fetchone()["metadata"]
    assert set(stored) <= set(oauth.METADATA_FIELDS)
    assert stored["client_name"] == "MyApp" and stored["redirect_uris"] == [CLAUDE_CALLBACK]
    assert "ops@example.com" not in json.dumps(stored)


@pytest.mark.anyio
async def test_metadata_over_the_size_cap_is_refused(client_for, conn):
    async with client_for() as client:
        response = await register(client, software_id="x" * (oauth.MAX_METADATA + 10))
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"
    assert count(conn, "oauth_clients") == 0


@pytest.mark.anyio
async def test_register_bodies_over_16_kb_get_413(client_for, conn):
    big = json.dumps({"redirect_uris": [CLAUDE_CALLBACK], "client_name": "x" * (oauth.MAX_REGISTER_BODY + 1)})

    async def chunks():
        yield big.encode()

    async with client_for() as client:
        declared = await client.post("/register", content=big, headers={"content-type": "application/json"})
        streamed = await client.post("/register", content=chunks(), headers={"content-type": "application/json"})
    assert declared.status_code == 413
    assert streamed.status_code == 413 and "content-length" not in streamed.request.headers
    assert count(conn, "oauth_clients") == 0


@pytest.mark.parametrize(("scope", "expected"), [(None, "pay"), ("pay", "pay"), ("pay offline_access", "pay"),
                                                 ("offline_access", "pay")])
@pytest.mark.anyio
async def test_registered_scope_is_normalized_to_pay(client_for, scope, expected):
    extra = {} if scope is None else {"scope": scope}
    async with client_for() as client:
        assert (await registered(client, **extra))["scope"] == expected


@pytest.mark.anyio
async def test_unknown_scopes_are_refused_at_registration(client_for):
    async with client_for() as client:
        response = await register(client, scope="pay admin")
    assert response.status_code == 400


@pytest.mark.anyio
async def test_the_per_ip_registration_limit_answers_429_with_retry_after(client_for, conn, settings):
    fill_clients(conn, 20, rl.ip_hash(settings, IP))
    async with client_for(IP) as client:
        limited = await register(client)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert count(conn, "oauth_clients") == 20
    async with client_for("198.51.100.7") as other:
        assert (await register(other)).status_code == 201


@pytest.mark.anyio
async def test_the_per_ip_limit_counts_only_the_last_hour(client_for, conn, settings):
    fill_clients(conn, 20, rl.ip_hash(settings, IP))
    conn.execute("UPDATE oauth_clients SET created_at = created_at - interval '61 minutes'")
    async with client_for(IP) as client:
        assert (await register(client)).status_code == 201


@pytest.mark.anyio
async def test_anthropics_egress_range_is_exempt_from_the_per_ip_limit(client_for, conn, settings):
    fill_clients(conn, 25, rl.ip_hash(settings, ANTHROPIC_IP))
    async with client_for(ANTHROPIC_IP) as client:
        assert (await register(client)).status_code == 201


@pytest.mark.anyio
async def test_500_ordinary_registrations_do_not_block_claude_ai(client_for, conn, settings):
    fill_clients(conn, 500)
    async with client_for(IP) as client:
        response = await register(client)
    assert response.status_code == 429 and int(response.headers["retry-after"]) >= 1
    async with client_for(ANTHROPIC_IP) as client:
        reg = await registered(client)
    stored = conn.execute("SELECT created_ip_hash FROM oauth_clients WHERE client_id = %s",
                          (reg["client_id"],)).fetchone()["created_ip_hash"]
    assert stored == rl.EXEMPT_MARK + rl.registration_ip_hash(settings, ANTHROPIC_IP)
    assert count(conn, "oauth_clients") == 501


@pytest.mark.anyio
async def test_exempt_registrations_have_their_own_500_cap(client_for, conn, settings):
    fill_clients(conn, 500, rl.EXEMPT_MARK + rl.registration_ip_hash(settings, ANTHROPIC_IP))
    async with client_for(ANTHROPIC_IP) as client:
        assert (await register(client)).status_code == 429
    async with client_for(IP) as client:
        assert (await register(client)).status_code == 201


@pytest.mark.anyio
async def test_registrations_are_limited_per_ipv6_48(client_for, conn, settings):
    fill_clients(conn, 20, rl.registration_ip_hash(settings, "2001:db8:1:2::1"))
    async with client_for("2001:db8:1:ffff::5") as client:  # another /64 in the same /48
        assert (await register(client)).status_code == 429
    async with client_for("2001:db8:2::1") as client:
        assert (await register(client)).status_code == 201


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded", "application/jsonx"])
@pytest.mark.anyio
async def test_register_refuses_anything_but_json_with_415(client_for, conn, content_type):
    body = json.dumps({"redirect_uris": [CLAUDE_CALLBACK]})
    headers = {} if content_type is None else {"content-type": content_type}
    async with client_for() as client:
        response = await client.post("/register", content=body, headers=headers)
        charset = await client.post("/register", content=body,
                                    headers={"content-type": "Application/JSON; charset=utf-8"})
    assert response.status_code == 415
    assert charset.status_code == 201
    assert count(conn, "oauth_clients") == 1


@pytest.mark.anyio
async def test_registration_purges_never_used_clients_older_than_30_days(client_for, conn):
    fill_clients(conn, 2, "old")
    conn.execute("UPDATE oauth_clients SET created_at = now() - interval '31 days'")
    kept = conn.execute("SELECT client_id FROM oauth_clients LIMIT 1").fetchone()["client_id"]
    conn.execute("UPDATE oauth_clients SET last_used_at = now() - interval '20 days' WHERE client_id = %s", (kept,))
    async with client_for() as client:
        await registered(client)
    ids = {row["client_id"] for row in conn.execute("SELECT client_id FROM oauth_clients").fetchall()}
    assert kept in ids and len(ids) == 2


# SentinelClient rules


def test_loopback_redirects_may_use_any_port_with_the_same_host_and_path():
    client = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(LOOPBACK_CALLBACK)])
    other_port = AnyUrl("http://localhost:5555/callback")
    assert client.validate_redirect_uri(other_port) == other_port
    for bad in ["http://localhost:5555/other", "http://127.0.0.1:33418/callback", "https://localhost:5555/callback",
                "http://localhost:5555/callback?x=1"]:
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(bad))


def test_other_redirects_must_match_exactly():
    client = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(CLAUDE_CALLBACK)])
    assert str(client.validate_redirect_uri(AnyUrl(CLAUDE_CALLBACK))) == CLAUDE_CALLBACK
    assert str(client.validate_redirect_uri(None)) == CLAUDE_CALLBACK
    for bad in ["https://claude.ai:8443/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback/x",
                "https://claude.com/api/mcp/auth_callback", "http://claude.ai/api/mcp/auth_callback"]:
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(bad))
    two = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(CLAUDE_CALLBACK), AnyUrl(LOOPBACK_CALLBACK)])
    with pytest.raises(InvalidRedirectUriError):
        two.validate_redirect_uri(None)


def test_scopes_pay_and_offline_access_become_pay():
    client = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(CLAUDE_CALLBACK)])
    assert client.validate_scope(None) is None
    assert client.validate_scope("pay") == ["pay"]
    assert client.validate_scope("pay offline_access") == ["pay"]
    with pytest.raises(InvalidScopeError):
        client.validate_scope("pay admin")


# Redirect helpers and resources


def test_trusted_redirects_are_exactly_claude_callbacks_or_loopback_http():
    assert all(oauth.is_trusted_redirect(uri) for uri in oauth.CLAUDE_CALLBACKS)
    assert oauth.is_trusted_redirect("http://localhost:6274/oauth/callback")
    assert oauth.is_trusted_redirect("http://127.0.0.1:33418/cb")
    for uri in [UNTRUSTED_CALLBACK, "https://claude.ai/api/mcp/auth_callback2", "https://claude.ai/other",
                "https://localhost/cb", "http://localhost@evil.example/cb", "cursor://x/cb", "http://example.com/",
                "http://app.localhost:6274/cb", "http://127.0.0.2:6274/cb"]:
        assert not oauth.is_trusted_redirect(uri), uri


def test_redirect_display_and_form_action_source():
    assert oauth.redirect_display(CLAUDE_CALLBACK) == "claude.ai"
    assert oauth.redirect_display("http://localhost:6274/cb") == "localhost:6274"
    assert oauth.redirect_display("https://xn--pple-43d.com/cb") == "xn--pple-43d.com"
    assert oauth.redirect_display("https://аpple.com/cb") == "xn--pple-43d.com"  # Cyrillic a, shown as punycode
    assert oauth.redirect_display("cursor://anysphere.cursor-retrieval/cb") == "cursor://anysphere.cursor-retrieval/cb"
    assert oauth.redirect_display("com.example.app:/cb") == "com.example.app:/cb"
    assert oauth.form_action_source(CLAUDE_CALLBACK) == "https://claude.ai"
    assert oauth.form_action_source("http://localhost:6274/cb") == "http://localhost:6274"
    assert oauth.form_action_source("cursor://anysphere.cursor-retrieval/cb") == "cursor:"
    assert oauth.form_action_source("http://[::1]:8080/cb") == "http://[::1]:8080"


@pytest.mark.parametrize(
    ("resource", "ok"),
    [
        (None, True), (RESOURCE, True), (RESOURCE + "/", True), ("http://LOCALHOST:8000/mcp", True),
        ("HTTP://localhost:8000/mcp", True), (RESOURCE + "//", False), ("http://localhost:8001/mcp", False),
        ("https://localhost:8000/mcp", False), ("http://127.0.0.1:8000/mcp", False), (TEST_BASE_URL, False),
        ("https://evil.example/mcp", False), (RESOURCE + "?x=1", False), (RESOURCE + "#x", False),
        ("http://user@localhost:8000/mcp", False), ("", False), ("mcp", False),
    ],
)
def test_canonical_resource(settings, resource, ok):
    assert oauth.canonical_resource(settings, resource) == (RESOURCE if ok else None)


# Pending requests


def test_pending_requests_round_trip(settings):
    pending = pending_request(state=None)
    req = oauth.sign_pending(settings, pending)
    assert auth.safe_next("/oauth/consent?req=" + req) is not None  # fits the sign in next allow list
    assert oauth.read_pending(settings, req) == pending
    unicode_state = pending_request(state="état ☕")
    assert oauth.read_pending(settings, oauth.sign_pending(settings, unicode_state)) == unicode_state


def test_tampered_foreign_malformed_or_oversized_pending_requests_are_refused(settings):
    req = oauth.sign_pending(settings, pending_request())
    payload, signature = req.split(".")
    middle = len(payload) // 2  # the signature covers the encoded string, so any edit must be refused
    flipped = payload[:middle] + ("A" if payload[middle] != "A" else "B") + payload[middle + 1:]
    other = load_settings(
        {"PUBLIC_BASE_URL": TEST_BASE_URL, "SESSION_SECRET": "o" * 40, "TOKEN_HASH_SECRET": "t" * 40}, "127.0.0.1"
    )
    forged_body = auth.b64(json.dumps({"v": 2, **pending_request().__dict__}).encode())
    for bad in [None, "", "garbage", f"{flipped}.{signature}", f"{payload}.{signature[:-1]}x", payload,
                oauth.sign_pending(other, pending_request()), "x" * (oauth.MAX_PENDING + 1),
                f"{forged_body}.{oauth._pending_mac(settings, forged_body)}", f"{payload}.{signature}.x"]:
        assert oauth.read_pending(settings, bad) is None


def test_expired_pending_requests_are_refused(settings, monkeypatch):
    req = oauth.sign_pending(settings, pending_request())
    now = oauth.clock()
    monkeypatch.setattr(oauth, "clock", lambda: now + oauth.PENDING_TTL_S + 1)
    assert oauth.read_pending(settings, req) is None


def test_binding_cookie(settings):
    https_settings = dataclasses.replace(settings, base_url="https://sentinel.example.com")
    nonce = "abcdefghijklmnop_-qrstuvwxyz0123"
    assert oauth.binding_cookie_name(settings, nonce) == "sentinel_consent_abcdefghijklmnop"
    assert oauth.binding_cookie_name(https_settings, nonce) == "__Host-sentinel_consent_abcdefghijklmnop"
    assert oauth.binding_cookie_name(settings, "n1") != oauth.binding_cookie_name(settings, "n2")
    assert oauth.binding_value(settings, "n1") == oauth.binding_value(settings, "n1")
    assert oauth.binding_value(settings, "n1") != oauth.binding_value(settings, "n2")


# /authorize


@pytest.mark.parametrize("resource", [None, RESOURCE, RESOURCE + "/"])
@pytest.mark.anyio
async def test_authorize_accepts_our_resource_absent_or_with_a_trailing_slash(client_for, settings, resource):
    async with client_for() as client:
        reg = await registered(client)
        _, challenge = pkce()
        response = await client.get("/authorize", params=authorize_query(reg, challenge, resource=resource))
    assert response.status_code == 302
    pending = pending_of(settings, response)
    assert pending.resource == RESOURCE and pending.client_id == reg["client_id"]
    assert pending.code_challenge == challenge and pending.state == "state-123"
    assert pending.redirect_uri == CLAUDE_CALLBACK and pending.redirect_uri_provided_explicitly


@pytest.mark.anyio
async def test_the_consent_redirect_sets_the_binding_cookie(client_for, settings):
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1]))
    pending = pending_of(settings, response)
    cookie = response.headers["set-cookie"]
    name = "sentinel_consent_" + pending.nonce[:16]
    assert cookie.startswith(f"{name}={oauth.binding_value(settings, pending.nonce)};")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered and "max-age=1200" in lowered


@pytest.mark.anyio
async def test_a_foreign_resource_is_refused_with_invalid_target_and_iss(client_for):
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get(
            "/authorize", params=authorize_query(reg, pkce()[1], resource="https://evil.example/mcp")
        )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    query = query_of(location)
    assert query["error"] == "invalid_target" and query["iss"] == TEST_BASE_URL and query["state"] == "state-123"
    assert "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_a_state_over_512_characters_is_refused(client_for):
    async with client_for() as client:
        reg = await registered(client, redirect_uris=(LOOPBACK_CALLBACK,))
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1], state="s" * 513))
        fits = await client.get("/authorize", params=authorize_query(reg, pkce()[1], state="s" * 512))
    assert response.status_code == 302
    assert query_of(response.headers["location"])["error"] == "invalid_request"
    assert fits.headers["location"].startswith(TEST_BASE_URL + "/oauth/consent?")


@pytest.mark.anyio
async def test_an_error_for_an_untrusted_redirect_renders_a_page_instead_of_redirecting(client_for):
    async with client_for() as client:
        reg = await registered(client, redirect_uris=(UNTRUSTED_CALLBACK,))
        response = await client.get(
            "/authorize", params=authorize_query(reg, pkce()[1], resource="https://evil.example/mcp")
        )
        scope = await client.get("/authorize", params=authorize_query(reg, pkce()[1], scope="admin"))
    for page in (response, scope):
        assert page.status_code == 400 and "location" not in page.headers
        assert page.headers["content-type"].startswith("text/html")
        assert "app.example.com" not in page.text
    assert "invalid_target" in response.text and "invalid_scope" in scope.text


@pytest.mark.anyio
async def test_errors_for_trusted_loopback_and_claude_redirects_go_back_with_iss(client_for):
    async with client_for() as client:
        for uri in (LOOPBACK_CALLBACK, CLAUDE_CALLBACK):
            reg = await registered(client, redirect_uris=(uri,))
            response = await client.get("/authorize", params=authorize_query(reg, pkce()[1], scope="admin"))
            assert response.status_code == 302
            location = response.headers["location"]
            assert location.startswith(uri + "?")
            assert query_of(location)["error"] == "invalid_scope" and query_of(location)["iss"] == TEST_BASE_URL


@pytest.mark.anyio
async def test_unknown_clients_and_bad_requests_get_the_error_page(client_for):
    async with client_for() as client:
        unknown = await client.get(
            "/authorize", params={"response_type": "code", "client_id": str(uuid.uuid4()),
                                  "redirect_uri": CLAUDE_CALLBACK, "code_challenge": pkce()[1]},
            headers={"accept": "text/html"},
        )
        reg = await registered(client)
        wrong_redirect = await client.get(
            "/authorize", params=authorize_query(reg, pkce()[1], redirect_uri="https://claude.ai/other")
        )
    for page in (unknown, wrong_redirect):
        assert page.status_code == 400 and "location" not in page.headers
        assert "This connection request cannot continue" in page.text


@pytest.mark.anyio
async def test_a_loopback_client_may_authorize_on_another_port(client_for, settings):
    async with client_for() as client:
        reg = await registered(client, redirect_uris=(LOOPBACK_CALLBACK,), auth_method="none")
        response = await client.get(
            "/authorize", params=authorize_query(reg, pkce()[1], redirect_uri="http://localhost:41000/callback")
        )
    assert pending_of(settings, response).redirect_uri == "http://localhost:41000/callback"


@pytest.mark.anyio
async def test_authorize_callback_refuses_a_malformed_challenge(provider, client_for):
    async with client_for() as client:
        reg = await registered(client)
    client_info = await provider.get_client(reg["client_id"])
    assert client_info is not None
    params = AuthorizationParams(
        state=None, scopes=["pay"], code_challenge="short", redirect_uri=AnyUrl(CLAUDE_CALLBACK),
        redirect_uri_provided_explicitly=True, resource=None,
    )
    with pytest.raises(AuthorizeError) as info:
        await provider.authorize(client_info, params)
    assert info.value.error == "invalid_request"


# approve and deny


@pytest.mark.anyio
async def test_approve_issues_one_code_per_consent_request(client_for, conn, settings, user):
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1]))
    pending = pending_of(settings, response)
    first = oauth.approve(conn, settings, pending, user.user_id, session_epoch=0)
    assert first is not None
    assert oauth.approve(conn, settings, pending, user.user_id, session_epoch=0) is None
    assert count(conn, "oauth_codes") == 1
    query = query_of(first)
    assert first.startswith(CLAUDE_CALLBACK + "?")
    assert auth.TOKEN_RE.fullmatch(query["code"]) and query["state"] == "state-123" and query["iss"] == TEST_BASE_URL
    row = grant_row(conn, reg["client_id"])
    assert row["code_hash"] == auth.digest(settings.key("auth-code"), query["code"])
    assert str(row["user_id"]) == user.user_id and row["scopes"] == ["pay"] and row["resource"] == RESOURCE
    ttl = conn.execute("SELECT expires_at - created_at AS code, grant_expires_at - created_at AS g"
                       " FROM oauth_codes").fetchone()
    assert ttl["code"].total_seconds() == 300 and ttl["g"].days == 90
    assert conn.execute("SELECT last_used_at FROM oauth_clients WHERE client_id = %s",
                        (reg["client_id"],)).fetchone()["last_used_at"] is not None


@pytest.mark.anyio
async def test_approve_sends_state_only_when_the_app_sent_one(client_for, conn, settings, user):
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1], state=None))
    location = oauth.approve(conn, settings, pending_of(settings, response), user.user_id, session_epoch=0)
    assert location is not None
    assert set(query_of(location)) == {"code", "iss"}


@pytest.mark.anyio
async def test_approve_refuses_a_user_without_a_wallet(client_for, conn, settings):
    walletless = new_user(conn, wallet=False)
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1]))
    with pytest.raises(ValueError):
        oauth.approve(conn, settings, pending_of(settings, response), walletless, session_epoch=0)
    assert count(conn, "oauth_codes") == 0


def test_deny_redirect_carries_access_denied_state_and_iss(settings):
    location = oauth.deny_redirect(settings, pending_request(state="abc"))
    query = query_of(location)
    assert location.startswith(CLAUDE_CALLBACK + "?")
    assert query["error"] == "access_denied" and query["state"] == "abc" and query["iss"] == TEST_BASE_URL
    assert "state" not in query_of(oauth.deny_redirect(settings, pending_request(state=None)))


# Codes


@pytest.mark.anyio
async def test_a_code_exchanges_for_a_bearer_pair(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["scope"] == "pay"
    assert oauth.ACCESS_RE.fullmatch(tokens["access_token"]) and oauth.REFRESH_RE.fullmatch(tokens["refresh_token"])
    access = await provider.load_access_token(tokens["access_token"])
    assert access is not None and access.subject == user.user_id
    assert grant_row(conn, ctx["reg"]["client_id"])["used_at"] is not None


@pytest.mark.anyio
async def test_a_public_client_exchanges_without_a_secret(client_for, conn, settings, user):
    async with client_for() as client:
        ctx = await code_for(client, conn, settings, user.user_id, redirect_uris=(LOOPBACK_CALLBACK,),
                             auth_method="none")
        response = await exchange(client, ctx, client_secret="")
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_a_replayed_code_revokes_the_grant_and_its_tokens(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
        replay = await exchange(client, ctx)
    assert replay.status_code == 401 and replay.json()["error"] == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(tokens["access_token"]) is None
    async with client_for() as client:
        assert (await refresh(client, ctx, tokens["refresh_token"])).status_code == 401


@pytest.mark.anyio
async def test_an_expired_code_is_refused(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx = await code_for(client, conn, settings, user.user_id)
        client_info = await provider.get_client(ctx["reg"]["client_id"])
        assert client_info is not None
        loaded = await provider.load_authorization_code(client_info, ctx["code"])
        assert loaded is not None
        conn.execute("UPDATE oauth_codes SET expires_at = now() - interval '1 second'")
        response = await exchange(client, ctx)
    assert response.status_code == 401 and response.json()["error"] == "invalid_grant"
    with pytest.raises(TokenError) as info:
        await provider.exchange_authorization_code(client_info, loaded)
    assert info.value.error == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is None  # expiry alone revokes nothing
    assert count(conn, "oauth_tokens") == 0


@pytest.mark.anyio
async def test_a_code_works_only_for_its_own_client(client_for, conn, settings, user):
    async with client_for() as client:
        ctx = await code_for(client, conn, settings, user.user_id)
        other = await registered(client)
        response = await exchange(client, ctx, client_id=other["client_id"], client_secret=other["client_secret"])
        still_good = await exchange(client, ctx)
    assert response.status_code == 401 and response.json()["error"] == "invalid_grant"
    assert still_good.status_code == 200


@pytest.mark.anyio
async def test_pkce_redirect_and_client_secret_mismatches_are_refused(client_for, conn, settings, user):
    async with client_for() as client:
        ctx = await code_for(client, conn, settings, user.user_id)
        wrong_verifier = await exchange(client, ctx, code_verifier=pkce()[0])
        wrong_redirect = await exchange(client, ctx, redirect_uri="https://claude.com/api/mcp/auth_callback")
        wrong_secret = await exchange(client, ctx, client_secret="0" * 64)
        unknown_client = await exchange(client, ctx, client_id=str(uuid.uuid4()))
        good = await exchange(client, ctx)
    assert wrong_verifier.status_code == 401 and wrong_verifier.json()["error"] == "invalid_grant"
    assert wrong_redirect.status_code == 400 and wrong_redirect.json()["error"] == "invalid_request"
    assert wrong_secret.status_code == 401 and wrong_secret.json()["error"] == "invalid_client"
    assert unknown_client.status_code == 401
    assert good.status_code == 200


@pytest.mark.anyio
async def test_malformed_codes_are_refused_before_any_sql(provider, client_for):
    async with client_for() as client:
        reg = await registered(client)
    client_info = await provider.get_client(reg["client_id"])
    assert client_info is not None
    for bad in ["", "short", "x" * 44, "a" * 42 + "!", "snt_" + "a" * 43]:
        assert await provider.load_authorization_code(client_info, bad) is None


# Refresh


@pytest.mark.anyio
async def test_refresh_rotates_the_pair(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        response = await refresh(client, ctx, first["refresh_token"])
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["access_token"] != first["access_token"] and second["refresh_token"] != first["refresh_token"]
    assert second["expires_in"] == 3600 and second["scope"] == "pay" and second["token_type"] == "Bearer"
    assert (second["access_token"], second["refresh_token"]) == oauth.rotated_pair(settings, first["refresh_token"])
    assert await provider.load_access_token(second["access_token"]) is not None
    used = conn.execute("SELECT used_at FROM oauth_tokens WHERE token_hash = %s",
                        (auth.digest(settings.key("refresh-token"), first["refresh_token"]),)).fetchone()
    assert used["used_at"] is not None


@pytest.mark.anyio
async def test_a_retry_within_the_grace_returns_the_same_pair(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        one = await refresh(client, ctx, first["refresh_token"])
        conn.execute("UPDATE oauth_tokens SET used_at = used_at - interval '20 seconds' WHERE used_at IS NOT NULL")
        two = await refresh(client, ctx, first["refresh_token"])
    assert one.status_code == two.status_code == 200
    assert one.json()["access_token"] == two.json()["access_token"]
    assert one.json()["refresh_token"] == two.json()["refresh_token"]
    assert count(conn, "oauth_tokens") == 4  # the first pair and one rotated pair, never a fork
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is None


@pytest.mark.anyio
async def test_reuse_after_the_grace_revokes_the_grant(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        rotated = (await refresh(client, ctx, first["refresh_token"])).json()
        conn.execute("UPDATE oauth_tokens SET used_at = used_at - interval '31 seconds' WHERE used_at IS NOT NULL")
        reuse = await refresh(client, ctx, first["refresh_token"])
        after = await refresh(client, ctx, rotated["refresh_token"])
    assert reuse.status_code == 401 and reuse.json()["error"] == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(rotated["access_token"]) is None
    assert after.status_code == 401


@pytest.mark.anyio
async def test_refresh_under_an_expired_grant_is_refused(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        conn.execute("UPDATE oauth_codes SET grant_expires_at = now() - interval '1 second'")
        response = await refresh(client, ctx, first["refresh_token"])
    assert response.status_code == 401 and response.json()["error"] == "invalid_grant"
    assert count(conn, "oauth_tokens") == 2


@pytest.mark.anyio
async def test_a_refresh_token_works_only_for_its_own_client(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        other = await registered(client)
        response = await refresh(client, {"reg": other}, first["refresh_token"])
    assert response.status_code == 401
    assert count(conn, "oauth_tokens") == 2


@pytest.mark.anyio
async def test_tokens_never_outlive_the_grant(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        conn.execute("UPDATE oauth_codes SET grant_expires_at = now() + interval '10 minutes'")
        response = await refresh(client, ctx, first["refresh_token"])
    assert response.status_code == 200 and response.json()["expires_in"] == 600
    new = [auth.digest(settings.key(kind), response.json()[field]) for kind, field in
           (("access-token", "access_token"), ("refresh-token", "refresh_token"))]
    rows = conn.execute("SELECT t.expires_at = g.grant_expires_at AS capped FROM oauth_tokens t"
                        " JOIN oauth_codes g ON g.id = t.grant_id WHERE t.token_hash = ANY(%s)", (new,)).fetchall()
    assert len(rows) == 2 and all(row["capped"] for row in rows)


# Access tokens


@pytest.mark.anyio
async def test_a_live_access_token_resolves_to_its_user_agent_and_wallet(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
    access = await provider.load_access_token(tokens["access_token"])
    assert access is not None
    assert access.subject == user.user_id and access.client_id == ctx["reg"]["client_id"]
    assert access.scopes == ["pay"] and access.resource == RESOURCE
    assert access.claims == {"iss": TEST_BASE_URL, "sub": user.user_id, "agent": user.agent, "wallet": user.wallet}
    assert users.Identity.from_claims(access.claims) == user
    assert access.expires_at is not None
    assert await provider.verify_token(tokens["access_token"]) == access


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE oauth_tokens SET expires_at = now() - interval '1 second' WHERE kind = 'access'",
        "UPDATE oauth_codes SET revoked_at = now()",
        "UPDATE oauth_codes SET grant_expires_at = now() - interval '1 second'",
        "UPDATE oauth_codes SET resource = 'https://other.example/mcp'",
    ],
    ids=["expired", "revoked", "grant past 90 days", "foreign resource"],
)
@pytest.mark.anyio
async def test_dead_access_tokens_resolve_to_nothing(client_for, conn, settings, user, provider, sql):
    async with client_for() as client:
        _, tokens = await tokens_for(client, conn, settings, user.user_id)
    conn.execute(sql)
    assert await provider.load_access_token(tokens["access_token"]) is None


@pytest.mark.anyio
async def test_a_token_for_a_user_without_a_wallet_resolves_to_nothing(client_for, conn, settings, provider):
    walletless = new_user(conn, wallet=False)
    async with client_for() as client:
        reg = await registered(client)
    grant_id = db.insert(
        conn,
        "INSERT INTO oauth_codes (code_hash, request_nonce, client_id, user_id, redirect_uri,"
        " redirect_uri_provided_explicitly, code_challenge, scopes, resource, expires_at, grant_expires_at)"
        " VALUES ('h', 'n', %s, %s, %s, true, 'c', '{pay}', %s, now(), now() + interval '90 days')",
        (reg["client_id"], walletless, CLAUDE_CALLBACK, RESOURCE),
    )
    tokens = oauth.issue_tokens(conn, settings, grant_id)
    assert await provider.load_access_token(tokens.access_token) is None


@pytest.mark.anyio
async def test_malformed_and_old_agent_tokens_resolve_to_nothing(provider):
    for bad in ["", "garbage", "snt_" + "a" * 43, "sat_" + "a" * 42, "sat_" + "a" * 44, "srt_" + "a" * 43,
                "sat_" + "a" * 42 + "\n", "Bearer sat_" + "a" * 43]:
        assert await provider.load_access_token(bad) is None


@pytest.mark.anyio
async def test_a_database_error_reads_as_not_authenticated(settings):
    @contextmanager
    def broken() -> Iterator[db.Connection]:
        raise db.DatabaseError("connection refused")
        yield  # pragma: no cover

    provider = oauth.SentinelOAuthProvider(settings, broken)
    assert await provider.load_access_token("sat_" + "a" * 43) is None


# Revocation


@pytest.mark.anyio
async def test_revoking_a_token_ends_the_whole_grant(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
        response = await client.post("/revoke", data={
            "token": tokens["refresh_token"], "token_type_hint": "refresh_token",
            "client_id": ctx["reg"]["client_id"], "client_secret": ctx["reg"]["client_secret"],
        })
        again = await refresh(client, ctx, tokens["refresh_token"])
    assert response.status_code == 200
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(tokens["access_token"]) is None
    assert again.status_code == 401


@pytest.mark.anyio
async def test_revoke_token_callback_with_an_access_token(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
    access = await provider.load_access_token(tokens["access_token"])
    assert access is not None
    await provider.revoke_token(access)
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(tokens["access_token"]) is None


# Discovery documents


@pytest.mark.anyio
async def test_authorization_server_metadata(client_for):
    async with client_for() as client:
        response = await client.get("/.well-known/oauth-authorization-server", headers={"origin": "https://x.test"})
    assert response.status_code == 200
    doc = response.json()
    assert doc["issuer"] == TEST_BASE_URL
    assert doc["authorization_endpoint"] == TEST_BASE_URL + "/authorize"
    assert doc["token_endpoint"] == TEST_BASE_URL + "/token"
    assert doc["registration_endpoint"] == TEST_BASE_URL + "/register"
    assert doc["revocation_endpoint"] == TEST_BASE_URL + "/revoke"
    assert doc["scopes_supported"] == ["pay"] and doc["response_types_supported"] == ["code"]
    assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]
    methods = ["none", "client_secret_post"]
    assert doc["token_endpoint_auth_methods_supported"] == methods
    assert doc["revocation_endpoint_auth_methods_supported"] == methods
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["authorization_response_iss_parameter_supported"] is True
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.anyio
async def test_protected_resource_metadata_at_both_paths(client_for):
    async with client_for() as client:
        docs = [(await client.get(path)).json() for path in
                ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource")]
    expected = {"resource": RESOURCE, "authorization_servers": [TEST_BASE_URL], "scopes_supported": ["pay"],
                "bearer_methods_supported": ["header"], "resource_name": "Sentinel"}
    assert docs == [expected, expected]


@pytest.mark.anyio
async def test_issuer_prm_and_iss_are_byte_identical(client_for):
    async with client_for() as client:
        issuer = json.loads((await client.get("/.well-known/oauth-authorization-server")).content)["issuer"]
        prm = json.loads((await client.get("/.well-known/oauth-protected-resource/mcp")).content)
        reg = await registered(client)
        error = await client.get("/authorize", params=authorize_query(reg, pkce()[1], scope="admin"))
    iss = query_of(error.headers["location"])["iss"]
    assert issuer == prm["authorization_servers"][0] == iss == TEST_BASE_URL
    assert not issuer.endswith("/")


def test_get_routes_keeps_token_and_revoke_and_refuses_other_mcp_paths(provider):
    paths = [route.path for route in provider.get_routes("/mcp")]
    for path in ["/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-protected-resource", "/authorize", "/token", "/register", "/revoke"]:
        assert paths.count(path) == 1, path
    assert provider.scopes_supported == ["pay"]
    with pytest.raises(ValueError):
        provider.get_routes("/other")


# Storage


@pytest.mark.anyio
async def test_the_database_holds_only_hashes(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        second = (await refresh(client, ctx, first["refresh_token"])).json()
    secrets_seen = [ctx["reg"]["client_secret"], ctx["code"], ctx["verifier"], first["access_token"],
                    first["refresh_token"], second["access_token"], second["refresh_token"]]
    dump = "".join(
        row["doc"] for table in ("oauth_clients", "oauth_codes", "oauth_tokens")
        for row in conn.execute(f"SELECT to_jsonb(t)::text AS doc FROM {table} t").fetchall()
    )
    assert dump
    for value in secrets_seen:
        assert value not in dump
    assert auth.digest(settings.key("auth-code"), ctx["code"]) in dump


@pytest.mark.anyio
async def test_mcp_without_a_token_gets_the_401_challenge(provider):
    app = FastMCP("challenge", auth=provider).http_app(path="/mcp", json_response=True, stateless_http=True)
    transport = httpx.ASGITransport(app=app, client=(IP, 50000))
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        bad = await client.post("/mcp", json={}, headers={"authorization": "Bearer snt_" + "a" * 43})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer scope="pay", resource_metadata="{TEST_BASE_URL}/.well-known/oauth-protected-resource/mcp"'
    )
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]


# Review fixes


@pytest.mark.anyio
async def test_client_secret_basic_is_registered_as_client_secret_post(client_for, conn, settings, user, provider):
    async with client_for() as client:
        reg = await registered(client, auth_method="client_secret_basic")
        assert reg["token_endpoint_auth_method"] == "client_secret_post"
        assert reg["client_secret"] == oauth.client_secret_for(settings, reg["client_id"])
        stored = conn.execute("SELECT token_endpoint_auth_method FROM oauth_clients WHERE client_id = %s",
                              (reg["client_id"],)).fetchone()["token_endpoint_auth_method"]
        assert stored == "client_secret_post"
        loaded = await provider.get_client(reg["client_id"])
        assert loaded is not None and loaded.token_endpoint_auth_method == "client_secret_post"
        verifier, challenge = pkce()
        response = await client.get("/authorize", params=authorize_query(reg, challenge))
        location = oauth.approve(conn, settings, pending_of(settings, response), user.user_id, session_epoch=0)
        assert location is not None
        ctx = {"reg": reg, "verifier": verifier, "code": query_of(location)["code"]}
        assert (await exchange(client, ctx)).status_code == 200


@pytest.mark.anyio
async def test_unknown_auth_methods_are_still_refused(client_for, conn):
    async with client_for() as client:
        response = await register(client, auth_method="tls_client_auth")
    assert response.status_code == 400 and count(conn, "oauth_clients") == 0


async def revoke(client: httpx.AsyncClient, token: str, form: dict[str, str]) -> httpx.Response:
    """POST /revoke for a token with these client credentials."""
    return await client.post("/revoke", data={"token": token, **form})


@pytest.mark.anyio
async def test_a_public_client_revokes_without_a_client_secret_field(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx = await code_for(client, conn, settings, user.user_id, redirect_uris=(LOOPBACK_CALLBACK,),
                             auth_method="none")
        tokens = (await exchange(client, ctx, client_secret="")).json()
        response = await revoke(client, tokens["access_token"], {"client_id": ctx["reg"]["client_id"]})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(tokens["access_token"]) is None


@pytest.mark.anyio
async def test_a_confidential_client_revokes_with_its_secret_and_only_with_it(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
        client_id = ctx["reg"]["client_id"]
        missing = await revoke(client, tokens["access_token"], {"client_id": client_id})
        wrong = await revoke(client, tokens["access_token"], {"client_id": client_id, "client_secret": "0" * 64})
        assert grant_row(conn, client_id)["revoked_at"] is None
        good = await revoke(client, tokens["access_token"],
                            {"client_id": client_id, "client_secret": ctx["reg"]["client_secret"]})
    for refused in (missing, wrong):
        assert refused.status_code == 401 and refused.json()["error"] == "invalid_client"
    assert good.status_code == 200
    assert grant_row(conn, client_id)["revoked_at"] is not None


@pytest.mark.anyio
async def test_a_client_cannot_revoke_another_clients_token(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
        other = await registered(client)
        creds = {"client_id": other["client_id"], "client_secret": other["client_secret"]}
        for token, hint in ((tokens["access_token"], "access_token"), (tokens["refresh_token"], "refresh_token")):
            assert (await revoke(client, token, {**creds, "token_type_hint": hint})).status_code == 200
        unknown = await revoke(client, "sat_" + "a" * 43, creds)
    assert unknown.status_code == 200
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is None
    assert await provider.load_access_token(tokens["access_token"]) is not None


@pytest.mark.anyio
async def test_refresh_may_ask_for_pay_and_offline_access(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        response = await client.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"], "scope": "pay offline_access",
            "client_id": ctx["reg"]["client_id"], "client_secret": ctx["reg"]["client_secret"],
        })
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "pay"
    row = conn.execute("SELECT scopes FROM oauth_codes WHERE client_id = %s", (ctx["reg"]["client_id"],)).fetchone()
    assert row["scopes"] == ["pay"]


@pytest.mark.parametrize("uri", [TEST_BASE_URL + "/oauth/consent", TEST_BASE_URL + "/cb", "http://LOCALHOST:8000/x"])
@pytest.mark.anyio
async def test_redirect_uris_on_sentinels_own_origin_are_refused(client_for, conn, uri):
    async with client_for() as client:
        response = await register(client, redirect_uris=(uri,))
    assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri"
    assert count(conn, "oauth_clients") == 0


def test_own_host_check_on_a_named_issuer():
    named = load_settings({"PUBLIC_BASE_URL": "https://sentinel.example", "SESSION_SECRET": "s" * 40,
                           "TOKEN_HASH_SECRET": "t" * 40, "RESEND_API_KEY": "k", "MAIL_FROM": "a@b.c"}, "0.0.0.0")
    assert oauth.on_issuer_host(named, "https://sentinel.example/cb")
    assert oauth.on_issuer_host(named, "https://sentinel.example:8443/cb")
    assert not oauth.on_issuer_host(named, "https://app.sentinel.example/cb")
    assert not oauth.on_issuer_host(named, CLAUDE_CALLBACK)


@pytest.mark.anyio
async def test_a_redirect_uri_that_looks_like_the_consent_page_gets_no_binding_cookie(client_for, conn, settings):
    """The old bypass: a client whose redirect URI is our own consent URL carrying a real signed request. Its error
    redirect used to start with the consent prefix and so received the binding cookie for someone else's request."""
    victim = oauth.sign_pending(settings, pending_request())
    bait = f"{TEST_BASE_URL}{oauth.CONSENT_PATH}?req={victim}"
    client_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
        " VALUES (%s, %s, 'none', 'x')",
        (client_id, json.dumps({"redirect_uris": [bait]})),
    )
    reg = {"client_id": client_id, "redirect_uris": [bait]}
    async with client_for() as client:
        bad_scope = await client.get("/authorize", params=authorize_query(reg, pkce()[1], scope="admin"))
        good_scope = await client.get("/authorize", params=authorize_query(reg, pkce()[1]))
    for response in (bad_scope, good_scope):
        assert "set-cookie" not in response.headers
        if "location" in response.headers:
            assert "error" in query_of(response.headers["location"])


@pytest.mark.parametrize(
    "uri",
    [
        "microsoft-edge:https://evil.example/cb", "microsoft-edge:http://evil.example/cb",
        "x-safari-https://evil.example/cb", "googlechrome://evil.example/cb", "googlechromes://evil.example/cb",
        "firefox://open-url?url=https://evil.example", "opera-http://evil.example/cb", "intent://evil.example#x",
        "x-web-search://?evil", "myapp:https://evil.example/cb", "myapp://https://evil.example/cb",
    ],
)
def test_schemes_that_wrap_another_url_are_refused(uri):
    assert oauth.redirect_uri_problem(uri) is not None


@pytest.mark.parametrize(
    "uri",
    ["vscode://anthropic.claude-code/callback", "cursor://anysphere.cursor-retrieval/oauth/callback",
     "com.example.app:/oauth2redirect", "com.example.app://oauth/cb"],
)
def test_app_schemes_still_work_and_show_in_full(uri):
    assert oauth.redirect_uri_problem(uri) is None
    assert oauth.redirect_display(uri) == uri


@pytest.mark.parametrize("uri", ["http://app.localhost:6274/cb", "http://localhost.:6274/cb",
                                 "http://127.0.0.2:6274/cb", "http://127.1.2.3/cb"])
def test_http_only_on_exactly_localhost_127_0_0_1_or_ipv6_loopback(uri):
    assert oauth.redirect_uri_problem(uri) is not None


@pytest.mark.parametrize("uri", ["https://app.localhost/cb", "https://localhost/callback", "https://localhost.:8443/cb",
                                 "https://a.b.LOCALHOST/cb", "cursor://app.localhost/cb", "http://app.localhost./cb"])
def test_localhost_names_are_refused_except_plain_http_on_exactly_localhost(uri):
    assert oauth.redirect_uri_problem(uri) is not None
    assert oauth.redirect_uri_problem("http://localhost:6274/cb") is None


def test_subdomains_of_localhost_get_no_port_flexibility():
    client = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl("http://app.localhost:33418/cb")])
    assert str(client.validate_redirect_uri(AnyUrl("http://app.localhost:33418/cb"))) == "http://app.localhost:33418/cb"
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl("http://app.localhost:5555/cb"))
    v6 = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl("http://[::1]:33418/cb")])
    assert str(v6.validate_redirect_uri(AnyUrl("http://[::1]:5555/cb"))) == "http://[::1]:5555/cb"


def test_requested_redirects_with_userinfo_or_fragment_are_refused():
    claude = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(CLAUDE_CALLBACK)])
    loopback = oauth.SentinelClient(client_id="c", redirect_uris=[AnyUrl(LOOPBACK_CALLBACK)])
    for client, bad in [
        (claude, "https://user@claude.ai/api/mcp/auth_callback"),
        (claude, "https://user:pw@claude.ai/api/mcp/auth_callback"),
        (claude, CLAUDE_CALLBACK + "#frag"),
        (loopback, "http://user@localhost:5555/callback"),
        (loopback, "http://localhost:5555/callback#frag"),
        (loopback, LOOPBACK_CALLBACK + "#frag"),
    ]:
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(bad))


@pytest.mark.anyio
async def test_approve_refuses_a_session_from_before_sign_out_everywhere(client_for, conn, settings, user):
    async with client_for() as client:
        reg = await registered(client)
        response = await client.get("/authorize", params=authorize_query(reg, pkce()[1]))
    pending = pending_of(settings, response)
    old_epoch = epoch(conn, user.user_id)
    users.sign_out_everywhere(conn, user.user_id)
    with pytest.raises(PermissionError, match="signed out"):
        oauth.approve(conn, settings, pending, user.user_id, session_epoch=old_epoch)
    assert count(conn, "oauth_codes") == 0
    assert oauth.approve(conn, settings, pending, user.user_id, session_epoch=old_epoch + 1) is not None


@pytest.mark.anyio
async def test_a_code_replayed_after_it_expired_still_revokes_the_grant(client_for, conn, settings, user, provider):
    async with client_for() as client:
        ctx, tokens = await tokens_for(client, conn, settings, user.user_id)
        conn.execute("UPDATE oauth_codes SET expires_at = now() - interval '1 minute'")
        replay = await exchange(client, ctx)
    assert replay.status_code == 401 and replay.json()["error"] == "invalid_grant"
    assert "already used" in replay.json()["error_description"]
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(tokens["access_token"]) is None


@pytest.mark.anyio
async def test_a_late_replay_still_needs_the_pkce_verifier(client_for, conn, settings, user):
    async with client_for() as client:
        ctx, _ = await tokens_for(client, conn, settings, user.user_id)
        conn.execute("UPDATE oauth_codes SET expires_at = now() - interval '1 minute'")
        replay = await exchange(client, ctx, code_verifier=pkce()[0])
    assert replay.status_code == 401
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is None


@pytest.mark.anyio
async def test_the_grace_does_not_serve_a_pair_whose_refresh_token_was_already_rotated(
    client_for, conn, settings, user, provider
):
    async with client_for() as client:
        ctx, first = await tokens_for(client, conn, settings, user.user_id)
        second = (await refresh(client, ctx, first["refresh_token"])).json()
        third = await refresh(client, ctx, second["refresh_token"])
        assert third.status_code == 200
        stale = await refresh(client, ctx, first["refresh_token"])  # still inside R0's 30 second grace
    assert stale.status_code == 401 and stale.json()["error"] == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await provider.load_access_token(third.json()["access_token"]) is None


# The full app over HTTP: the site, the consent page, /token, /revoke and /mcp on one origin (conftest app, browser)


def no_codes(conn: db.Connection) -> bool:
    """True when no consent has produced a code row."""
    return count(conn, "oauth_codes") == 0


def unique_email() -> str:
    """A fresh address, so sign in rate limits per address never interfere between steps."""
    return f"site-{uuid.uuid4().hex[:12]}@example.com"


async def start(client: httpx.AsyncClient, *, register_as: tuple[str, ...] = (CLAUDE_CALLBACK,), **kw: Any
                ) -> dict[str, Any]:
    """Register a client and send this browser to /authorize. kw goes to authorize_query (None leaves a parameter
    out; auth_method and client_name go to registration). Returns reg, verifier, the consent path and its pending
    request's nonce; the browser now holds the binding cookie."""
    reg_kw = {name: kw.pop(name) for name in ("auth_method", "client_name") if name in kw}
    reg = await registered(client, redirect_uris=register_as, **reg_kw)
    verifier, challenge = pkce()
    response = await client.get("/authorize", params=authorize_query(reg, challenge, **kw))
    assert response.status_code == 302, response.text
    path = path_of(response.headers["location"])
    assert path.startswith(oauth.CONSENT_PATH + "?req=")
    return {"reg": reg, "verifier": verifier, "path": path, "req": path.split("req=", 1)[1]}


async def to_consent(
    client: httpx.AsyncClient, mailer: RecordingMailer, email: str | None = None, **kw: Any
) -> dict[str, Any]:
    """start(), sign in coming back to the consent page, and load it. Adds the page, its csrf, the email, the link
    token the mail carried and the user id to the context."""
    ctx = await start(client, **kw)
    ctx["email"] = email or unique_email()
    signed_in = await sign_in(client, mailer, ctx["email"], next=ctx["path"])
    assert signed_in.headers["location"] == ctx["path"]
    ctx["link_token"] = mailer.last_token()
    page = await client.get(ctx["path"])
    assert page.status_code == 200, page.text
    assert form_value(page.text, "req") == ctx["req"]
    ctx["page"] = page
    ctx["csrf"] = form_value(page.text, "csrf")
    ctx["user_id"] = (client.cookies.get("sentinel_session") or "").split(".")[1]
    return ctx


async def answer(client: httpx.AsyncClient, ctx: dict[str, Any], action: str = "allow", **override: Any
                 ) -> httpx.Response:
    """POST the consent form (req, csrf, action); override replaces or (with None) drops a field."""
    form = {"req": ctx["req"], "csrf": ctx.get("csrf"), "action": action}
    form.update(override)
    return await client.post(oauth.CONSENT_PATH, data={key: value for key, value in form.items() if value is not None})


async def allowed(client: httpx.AsyncClient, ctx: dict[str, Any]) -> dict[str, Any]:
    """Allow, and add the redirect and its code to the context."""
    response = await answer(client, ctx)
    assert response.status_code == 303, response.text
    ctx["location"] = response.headers["location"]
    ctx["code"] = query_of(ctx["location"])["code"]
    return ctx


async def site_tokens(client: httpx.AsyncClient, mailer: RecordingMailer, **kw: Any
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The whole flow through the site up to the first token pair: (ctx, token response JSON)."""
    ctx = await allowed(client, await to_consent(client, mailer, **kw))
    response = await exchange(client, ctx)
    assert response.status_code == 200, response.text
    return ctx, dict(response.json())


async def mcp_status(client: httpx.AsyncClient, token: str) -> int:
    """The HTTP status of a tools/list call to /mcp with this bearer token."""
    response = await mcp_request(client, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return response.status_code


def nonce_of(settings: Settings, ctx: dict[str, Any]) -> str:
    """The nonce of the pending request in a consent context."""
    pending = oauth.read_pending(settings, ctx["req"])
    assert pending is not None
    return pending.nonce


def restore_binding(client: httpx.AsyncClient, settings: Settings, nonce: str) -> None:
    """Put the binding cookie for this request back in the browser (Allow and Deny clear it)."""
    value = oauth.binding_value(settings, nonce)
    # http.cookiejar files cookies from a dotless host such as localhost under "localhost.local"
    client.cookies.set(oauth.binding_cookie_name(settings, nonce), value, domain="localhost.local")



@pytest.mark.parametrize("state", ["state-123", None], ids=["with state", "without state"])
@pytest.mark.anyio
async def test_the_full_flow_through_the_site_ends_in_a_settled_payment(browser, mailer, pay_spy, state):
    client = browser()
    ctx = await allowed(client, await to_consent(client, mailer, state=state))
    assert ctx["location"].startswith(CLAUDE_CALLBACK + "?")
    params = query_of(ctx["location"])
    expected = {"code", "iss"} | ({"state"} if state else set())
    assert set(params) == expected and params["iss"] == TEST_BASE_URL and params.get("state") == state
    response = await exchange(client, ctx)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["scope"] == "pay"
    paid = await call_tool(client, tokens["access_token"], "pay", {"url": PAY_URL, "reason": REASON})
    assert paid["status"] == "SETTLED" and paid["body"] == PAID_BODY.decode()
    assert [call[2] for call in pay_spy.calls] == [f"user-{ctx['user_id']}"]


@pytest.mark.anyio
async def test_after_the_full_flow_the_database_holds_only_hashes(browser, mailer, conn, settings):
    client = browser()
    ctx, first = await site_tokens(client, mailer)
    second = (await refresh(client, ctx, first["refresh_token"])).json()
    raw = [ctx["link_token"], ctx["reg"]["client_secret"], ctx["code"], ctx["verifier"], first["access_token"],
           first["refresh_token"], second["access_token"], second["refresh_token"],
           client.cookies.get("sentinel_session")]
    tables = ["users", "magic_links", "oauth_clients", "oauth_codes", "oauth_tokens"]
    dump = {table: "".join(row["doc"] for row in conn.execute(f"SELECT to_jsonb(t)::text AS doc FROM {table} t"))
            for table in tables}
    assert all(dump.values()), [table for table, text in dump.items() if not text]
    for value in raw:
        assert value
        for table, text in dump.items():
            assert value not in text, f"a raw secret is stored in {table}"
    assert auth.digest(settings.key("auth-code"), ctx["code"]) in dump["oauth_codes"]


@pytest.mark.parametrize("binding", ["missing", "another request's"])
@pytest.mark.anyio
async def test_consent_needs_this_requests_binding_cookie(browser, mailer, conn, settings, binding):
    client = browser()
    ctx = await to_consent(client, mailer)
    name = oauth.binding_cookie_name(settings, nonce_of(settings, ctx))
    client.cookies.delete(name)
    if binding == "another request's":  # this request's cookie name carrying the value for another nonce
        client.cookies.set(name, oauth.binding_value(settings, "another-nonce"), domain="localhost.local")
    page = await client.get(ctx["path"])
    refused = await answer(client, ctx)
    for response in (page, refused):
        assert response.status_code == 400 and "location" not in response.headers
        assert "browser where you clicked Connect" in response.text
    assert no_codes(conn)


@pytest.mark.anyio
async def test_consent_without_a_session_goes_to_sign_in_and_back(browser, conn):
    client = browser()
    ctx = await start(client)
    page = await client.get(ctx["path"])
    posted = await answer(client, ctx, csrf="x")
    for response in (page, posted):
        assert response.status_code == 303
        assert response.headers["location"] == "/signin?" + urlencode({"next": ctx["path"]})
    assert no_codes(conn)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", None], ids=["foreign", "null", "missing"])
@pytest.mark.anyio
async def test_consent_posts_need_our_origin(browser, mailer, conn, origin):
    client = browser()
    ctx = await to_consent(client, mailer)
    del client.headers["origin"]
    headers = {} if origin is None else {"origin": origin}
    form = {"req": ctx["req"], "csrf": ctx["csrf"], "action": "allow"}
    refused = await client.post(oauth.CONSENT_PATH, data=form, headers=headers)
    assert refused.status_code == 403 and "location" not in refused.headers
    assert no_codes(conn)


@pytest.mark.parametrize("csrf", ["missing", "empty", "another session", "another nonce", "account"])
@pytest.mark.anyio
async def test_consent_posts_need_the_csrf_token_of_this_session_and_request(browser, mailer, conn, settings, csrf):
    client = browser()
    ctx = await to_consent(client, mailer)
    session = client.cookies.get("sentinel_session") or ""
    purpose = "consent:" + nonce_of(settings, ctx)
    if csrf == "another session":
        other = browser("198.51.100.20")
        await sign_in(other, mailer, unique_email())
        token = auth.csrf_token(settings, other.cookies.get("sentinel_session") or "", purpose)
    elif csrf == "another nonce":
        token = auth.csrf_token(settings, session, "consent:" + secrets.token_urlsafe(24))
    elif csrf == "account":
        token = auth.csrf_token(settings, session, "account")
    else:
        token = None if csrf == "missing" else ""
    assert token != ctx["csrf"]
    refused = await answer(client, ctx, csrf=token)
    assert refused.status_code == 403 and "location" not in refused.headers
    assert no_codes(conn)
    assert (await answer(client, ctx)).status_code == 303  # the right token still works afterwards


@pytest.mark.anyio
async def test_sign_out_everywhere_between_page_and_allow_refuses_the_consent(browser, mailer, conn):
    client = browser()
    ctx = await to_consent(client, mailer)
    other = browser("198.51.100.21")  # the same user, signed in on another device, signs out everywhere
    await sign_in(other, mailer, ctx["email"])
    account = await other.get("/account")
    assert account.status_code == 200
    out = await other.post("/account/signout-everywhere", data={"csrf": form_value(account.text, "csrf")})
    assert out.status_code == 303
    refused = await answer(client, ctx)
    assert refused.status_code == 303 and refused.headers["location"].startswith("/signin?")
    assert no_codes(conn)


@pytest.mark.parametrize(
    ("redirect_uri", "source"),
    [
        (CLAUDE_CALLBACK, "https://claude.ai"),
        ("http://localhost:6274/oauth/callback", "http://localhost:6274"),
        ("cursor://anysphere.cursor-retrieval/oauth/callback", "cursor:"),
    ],
)
@pytest.mark.anyio
async def test_the_consent_csp_lets_the_form_send_the_browser_to_the_redirect_origin(
    browser, mailer, redirect_uri, source
):
    client = browser()
    ctx = await to_consent(client, mailer, register_as=(redirect_uri,))
    policy = ctx["page"].headers["content-security-policy"]
    assert f"form-action 'self' {source};" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self';" in (await client.get("/signin")).headers["content-security-policy"]


@pytest.mark.anyio
async def test_the_app_supplied_name_is_sanitized_escaped_and_isolated_in_bdi(browser, mailer):
    client = browser()
    name = "Claude‮​ <script>alert(1)</script> & co"
    ctx = await to_consent(client, mailer, client_name=name)
    shown = re.findall(r"<bdi[^>]*>(.*?)</bdi>", ctx["page"].text)
    assert shown == ["Claude &lt;script&gt;alert(1)&lt;/script&gt; &amp; co"]
    assert "<script>alert" not in ctx["page"].text and "‮" not in ctx["page"].text


@pytest.mark.parametrize(
    ("redirect_uri", "caution"),
    [(UNTRUSTED_CALLBACK, True), (CLAUDE_CALLBACK, False), ("http://127.0.0.1:33418/callback", False),
     ("cursor://anysphere.cursor-retrieval/oauth/callback", True)],
)
@pytest.mark.anyio
async def test_the_caution_box_shows_unless_the_redirect_is_claude_or_loopback(browser, mailer, redirect_uri, caution):
    client = browser()
    ctx = await to_consent(client, mailer, register_as=(redirect_uri,))
    text = ctx["page"].text
    assert (">Caution<" in text) is caution
    assert oauth.redirect_display(redirect_uri) in text


@pytest.mark.parametrize("state", ["state-123", None])
@pytest.mark.anyio
async def test_deny_goes_back_with_access_denied_state_and_iss(browser, mailer, conn, settings, state):
    client = browser()
    ctx = await to_consent(client, mailer, state=state)
    denied = await answer(client, ctx, action="deny")
    assert denied.status_code == 303
    location = denied.headers["location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    params = query_of(location)
    assert params["error"] == "access_denied" and params["iss"] == TEST_BASE_URL and params.get("state") == state
    assert "code" not in params and no_codes(conn)
    assert oauth.binding_cookie_name(settings, nonce_of(settings, ctx)) not in client.cookies


@pytest.mark.anyio
async def test_an_unknown_action_is_refused(browser, mailer, conn):
    client = browser()
    ctx = await to_consent(client, mailer)
    assert (await answer(client, ctx, action="maybe")).status_code == 400
    assert (await answer(client, ctx, action=None)).status_code == 400
    assert no_codes(conn)


@pytest.mark.anyio
async def test_an_authorize_error_for_an_untrusted_redirect_is_a_page_on_the_site(browser, settings):
    client = browser()
    reg = await registered(client, redirect_uris=(UNTRUSTED_CALLBACK,))
    for query in (authorize_query(reg, pkce()[1], scope="admin"),
                  authorize_query(reg, pkce()[1], resource="https://evil.example/mcp")):
        response = await client.get("/authorize", params=query)
        assert response.status_code == 400 and "location" not in response.headers
        assert response.headers["content-type"].startswith("text/html")
        assert "This connection request cannot continue" in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "sentinel_consent" not in response.headers.get("set-cookie", "")


@pytest.mark.anyio
async def test_a_foreign_resource_is_refused_through_the_site(browser, settings):
    client = browser()
    reg = await registered(client)
    response = await client.get("/authorize", params=authorize_query(reg, pkce()[1],
                                                                     resource="https://evil.example/mcp"))
    assert response.status_code == 302 and response.headers["location"].startswith(CLAUDE_CALLBACK + "?")
    assert query_of(response.headers["location"])["error"] == "invalid_target"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("resource", [None, RESOURCE + "/"], ids=["missing", "trailing slash"])
@pytest.mark.anyio
async def test_a_missing_or_trailing_slash_resource_works_through_the_whole_flow(
    browser, mailer, conn, settings, resource
):
    client = browser()
    ctx, tokens = await site_tokens(client, mailer, resource=resource)
    assert grant_row(conn, ctx["reg"]["client_id"])["resource"] == RESOURCE
    paid = await call_tool(client, tokens["access_token"], "pay", {"url": PAY_URL, "reason": REASON})
    assert paid["status"] == "SETTLED"


@pytest.mark.anyio
async def test_a_loopback_app_may_use_another_port_through_the_whole_flow(browser, mailer, conn):
    client = browser()
    ctx = await to_consent(client, mailer, register_as=("http://127.0.0.1:3000/callback",), auth_method="none",
                           redirect_uri="http://127.0.0.1:4567/callback")
    await allowed(client, ctx)
    assert ctx["location"].startswith("http://127.0.0.1:4567/callback?")
    registered_port = await exchange(client, ctx, client_secret="", redirect_uri="http://127.0.0.1:3000/callback")
    assert registered_port.status_code == 400 and registered_port.json()["error"] == "invalid_request"
    response = await exchange(client, ctx, client_secret="", redirect_uri="http://127.0.0.1:4567/callback")
    assert response.status_code == 200, response.text
    assert await mcp_status(client, response.json()["access_token"]) == 200


@pytest.mark.parametrize("kind", ["tampered", "expired", "oversized"])
@pytest.mark.anyio
async def test_bad_pending_requests_are_refused_at_the_consent_page(
    browser, mailer, conn, settings, monkeypatch, kind
):
    client = browser()
    ctx = await to_consent(client, mailer)
    if kind == "tampered":
        payload, signature = ctx["req"].split(".")
        middle = len(payload) // 2
        flipped = payload[:middle] + ("A" if payload[middle] != "A" else "B") + payload[middle + 1:]
        ctx["req"] = f"{flipped}.{signature}"
    elif kind == "expired":
        now = oauth.clock()
        monkeypatch.setattr(oauth, "clock", lambda: now + oauth.PENDING_TTL_S + 1)
    else:  # validly signed and bound to this browser, but over the size cap
        pending = oauth.read_pending(settings, ctx["req"])
        assert pending is not None
        big = oauth.PendingRequest(**{**pending.__dict__, "state": "s" * oauth.MAX_PENDING})
        ctx["req"] = oauth.sign_pending(settings, big)
        assert len(ctx["req"]) > oauth.MAX_PENDING
        restore_binding(client, settings, big.nonce)
    page = await client.get(oauth.CONSENT_PATH, params={"req": ctx["req"]})
    posted = await answer(client, ctx)
    for response in (page, posted):
        assert response.status_code == 400 and "location" not in response.headers
        assert "This connection request does not work" in response.text
    assert no_codes(conn)


@pytest.mark.anyio
async def test_two_flows_in_one_browser_both_finish(browser, mailer, conn, settings):
    client = browser()
    first = await to_consent(client, mailer)
    second = await start(client)  # a second /authorize before the first is answered
    assert nonce_of(settings, first) != nonce_of(settings, second)
    page = await client.get(second["path"])
    assert page.status_code == 200, page.text
    second["csrf"] = form_value(page.text, "csrf")
    for ctx in (first, second):
        await allowed(client, ctx)
        assert oauth.binding_cookie_name(settings, nonce_of(settings, ctx)) not in client.cookies
    assert count(conn, "oauth_codes") == 2


@pytest.mark.anyio
async def test_one_code_per_consent(browser, mailer, conn, settings):
    client = browser()
    ctx = await allowed(client, await to_consent(client, mailer))
    assert oauth.binding_cookie_name(settings, nonce_of(settings, ctx)) not in client.cookies  # Allow clears it
    restore_binding(client, settings, nonce_of(settings, ctx))
    page = await client.get(ctx["path"])
    again = await answer(client, ctx)
    for response in (page, again):
        assert response.status_code == 409 and "location" not in response.headers
        assert "Already approved" in response.text
    assert count(conn, "oauth_codes") == 1


@pytest.mark.anyio
async def test_a_replayed_code_through_the_site_revokes_the_grant_and_mcp_then_refuses(browser, mailer, conn):
    client = browser()
    ctx, tokens = await site_tokens(client, mailer)
    assert await mcp_status(client, tokens["access_token"]) == 200
    replay = await exchange(client, ctx)
    assert replay.status_code == 401 and replay.json()["error"] == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await mcp_status(client, tokens["access_token"]) == 401
    assert (await refresh(client, ctx, tokens["refresh_token"])).status_code == 401


@pytest.mark.anyio
async def test_codes_at_the_site_token_endpoint_are_checked(browser, mailer, conn):
    client = browser()
    ctx = await allowed(client, await to_consent(client, mailer))
    wrong_verifier = await exchange(client, ctx, code_verifier=pkce()[0])
    wrong_redirect = await exchange(client, ctx, redirect_uri="https://claude.com/api/mcp/auth_callback")
    unknown_client = await exchange(client, ctx, client_id=str(uuid.uuid4()))
    assert wrong_verifier.status_code == 401 and wrong_verifier.json()["error"] == "invalid_grant"
    assert wrong_redirect.status_code == 400 and wrong_redirect.json()["error"] == "invalid_request"
    assert unknown_client.status_code == 401 and unknown_client.json()["error"] == "invalid_client"
    row = grant_row(conn, ctx["reg"]["client_id"])
    assert row["used_at"] is None and row["revoked_at"] is None  # none of them spent the code
    conn.execute("UPDATE oauth_codes SET expires_at = now() - interval '1 second'")
    expired = await exchange(client, ctx)
    assert expired.status_code == 401 and expired.json()["error"] == "invalid_grant"
    assert count(conn, "oauth_tokens") == 0


@pytest.mark.anyio
async def test_refresh_through_the_site_rotates_retries_within_the_grace_and_revokes_on_reuse(
    browser, mailer, conn, settings
):
    client = browser()
    ctx, first = await site_tokens(client, mailer)
    rotated = await refresh(client, ctx, first["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"] and second["access_token"] != first["access_token"]
    assert await mcp_status(client, second["access_token"]) == 200

    conn.execute("UPDATE oauth_tokens SET used_at = used_at - interval '20 seconds' WHERE used_at IS NOT NULL")
    retry = await refresh(client, ctx, first["refresh_token"])
    assert retry.status_code == 200 and retry.json() | {"expires_in": 0} == second | {"expires_in": 0}

    conn.execute("UPDATE oauth_tokens SET used_at = used_at - interval '11 seconds' WHERE used_at IS NOT NULL")
    reuse = await refresh(client, ctx, first["refresh_token"])
    assert reuse.status_code == 401 and reuse.json()["error"] == "invalid_grant"
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await mcp_status(client, second["access_token"]) == 401
    assert (await refresh(client, ctx, second["refresh_token"])).status_code == 401


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
@pytest.mark.anyio
async def test_revoke_through_the_site_ends_the_grant(browser, mailer, conn, which):
    client = browser()
    ctx, tokens = await site_tokens(client, mailer)
    assert await mcp_status(client, tokens["access_token"]) == 200
    response = await client.post("/revoke", data={
        "token": tokens[which], "token_type_hint": which,
        "client_id": ctx["reg"]["client_id"], "client_secret": ctx["reg"]["client_secret"],
    })
    assert response.status_code == 200
    assert grant_row(conn, ctx["reg"]["client_id"])["revoked_at"] is not None
    assert await mcp_status(client, tokens["access_token"]) == 401
    assert (await refresh(client, ctx, tokens["refresh_token"])).status_code == 401


@pytest.mark.anyio
async def test_the_90_day_cap_ends_access_and_refresh(browser, mailer, conn):
    client = browser()
    ctx, tokens = await site_tokens(client, mailer)
    conn.execute("UPDATE oauth_codes SET created_at = created_at - interval '90 days',"
                 " grant_expires_at = grant_expires_at - interval '90 days 1 second'")
    assert await mcp_status(client, tokens["access_token"]) == 401
    refreshed = await refresh(client, ctx, tokens["refresh_token"])
    assert refreshed.status_code == 401 and refreshed.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE oauth_tokens SET expires_at = now() - interval '1 second' WHERE kind = 'access'",
        "UPDATE oauth_codes SET revoked_at = now()",
        "UPDATE oauth_codes SET resource = 'https://other.example/mcp'",
        None,
    ],
    ids=["expired", "revoked", "foreign audience", "old snt_ agent token"],
)
@pytest.mark.anyio
async def test_dead_access_tokens_get_401_at_mcp(browser, mailer, conn, pay_spy, sql):
    client = browser()
    _, tokens = await site_tokens(client, mailer)
    token = tokens["access_token"]
    assert await mcp_status(client, token) == 200
    if sql is None:
        token = "snt_" + secrets.token_urlsafe(32)
    else:
        conn.execute(sql)
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "pay", "arguments": {"url": PAY_URL, "reason": REASON}}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert pay_spy.calls == []


@pytest.mark.anyio
async def test_a_real_fastmcp_client_lists_and_calls_the_tools(app, browser, mailer, pay_spy):
    _, tokens = await site_tokens(browser(), mailer)

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        """FastMCP's client factory hook: an httpx2 client that talks to the app in process, streaming."""
        options = {key: value for key, value in kwargs.items() if value is not None}
        return httpx2.AsyncClient(transport=StreamingASGITransport(app), **options)

    transport = StreamableHttpTransport(
        f"{TEST_BASE_URL}/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}"},
        httpx_client_factory=factory,
    )
    async with Client(transport) as mcp:
        tools = await mcp.list_tools()
        assert sorted(tool.name for tool in tools) == [
            "apify_actor", "apify_run_status", "compare_prices", "discover", "find_services", "pay", "payment_status",
            "quote", "run_apify_actor", "start_task",
        ]
        task_id = (await mcp.call_tool("start_task", {"task": TASK})).structured_content["task_id"]
        result = await mcp.call_tool("pay", {"task_id": task_id, "url": PAY_URL, "reason": REASON})
    assert result.structured_content is not None
    assert result.structured_content["status"] == "SETTLED"
    assert result.structured_content["body"] == PAID_BODY.decode()
    assert len(pay_spy.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("fresh,expected", [(False, "SETTLED"), (True, "BLOCKED")])
async def test_an_unknown_seller_is_decided_by_sentinel_over_authenticated_http_without_asking(
    app, browser, mailer, pay_spy, conn, fake_chain, fresh, expected,
):
    _, tokens = await site_tokens(browser(), mailer)
    pay_spy.pay_to = "0x" + "a" * 40
    if fresh:
        fake_chain.new.add(pay_spy.pay_to)

    async def answer(*args):
        pytest.fail("Sentinel never asks the human about a payment")

    def factory(**kwargs):
        options = {key: value for key, value in kwargs.items() if value is not None}
        return httpx2.AsyncClient(transport=StreamingASGITransport(app), **options)

    transport = StreamableHttpTransport(
        f"{TEST_BASE_URL}/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}"},
        httpx_client_factory=factory,
    )
    async with Client(transport, elicitation_handler=answer) as client:
        task_id = (await client.call_tool("start_task", {"task": TASK})).structured_content["task_id"]
        result = await client.call_tool("pay", {"task_id": task_id, "url": PAY_URL, "reason": REASON})
        assert result.structured_content["status"] == expected
    assert conn.execute("SELECT count(*) AS n FROM journal_entries").fetchone()["n"] == (expected == "SETTLED")
