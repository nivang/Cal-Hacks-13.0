"""Smoke tests for the whole app (web/app.py): one full connect and pay, and a few must hold properties.

The thorough HTTP suites live in test_web_auth.py, test_oauth.py and test_user_wallets.py; these only prove the
pieces are wired together: the site, the OAuth server and /mcp on one origin, under the app's lifespan.
"""
import pytest
from conftest import (
    DATACO,
    TEST_BASE_URL,
    call_tool,
    connect_flow,
    form_value,
    path_of,
    pkce,
    register,
    sign_in,
)

from sentinel import attempts
from sentinel.web import oauth

REASON = "Need 2026 email open-rate benchmarks for the Q3 report the owner asked for; DataCo is named in the brief."
URL = "http://seller.test/benchmarks/email"

pytestmark = pytest.mark.anyio


async def test_connect_then_pay_settles_through_the_users_own_wallet(browser, mailer, conn, pay_spy, fake_wallets):
    client = browser()
    result = await connect_flow(client, mailer, "Alice@Example.com")
    assert result.user_id and result.access_token.startswith("sat_")
    assert fake_wallets.opened == [(result.user_id, True)]  # created once, at first sign in

    paid = await call_tool(client, result.access_token, "pay", {"url": URL, "reason": REASON})
    assert paid["status"] == "SETTLED" and paid["body"] == '{"vendor":"DataCo","open_rate":0.214}'
    assert fake_wallets.opened[-1] == (result.user_id, False)  # paying only looks the wallet up
    (url, signer, agent, reason), = pay_spy.calls
    assert (url, agent, reason) == (URL, f"user-{result.user_id}", REASON)
    assert signer.address == fake_wallets.address_for(result.user_id)

    status = await call_tool(client, result.access_token, "payment_status", {"request_id": paid["request_id"]})
    assert status["status"] == "SETTLED" and status["request_id"] == paid["request_id"]
    row = attempts.get_attempt(conn, paid["request_id"], f"user-{result.user_id}")
    assert row is not None and row["status"] == "settled" and row["pay_to"].lower() == DATACO.lower()


async def test_mcp_without_a_token_gets_the_401_challenge_with_cors(browser):
    response = await browser().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer") and 'scope="pay"' in challenge
    assert f'resource_metadata="{TEST_BASE_URL}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert response.headers["access-control-allow-origin"] == "*"
    assert "WWW-Authenticate" in response.headers["access-control-expose-headers"]


async def test_mcp_preflight_is_answered_for_browser_clients(browser):
    response = await browser().options(
        "/mcp", headers={"access-control-request-method": "POST", "access-control-request-headers": "authorization"}
    )
    assert response.status_code == 204 and response.headers["access-control-allow-origin"] == "*"
    assert "mcp-protocol-version" in response.headers["access-control-allow-headers"]


async def test_healthz_needs_no_database(browser):
    response = await browser().get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_site_pages_carry_the_security_headers_and_mcp_does_not(browser):
    client = browser()
    page = await client.get("/signin")
    assert page.status_code == 200
    assert "form-action 'self'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY" and page.headers["referrer-policy"] == "same-origin"
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in page.headers  # http on localhost
    assert "content-security-policy" not in (await client.post("/mcp", json={})).headers


async def test_a_consent_post_without_the_binding_cookie_is_refused(browser, mailer, conn, settings):
    client = browser()
    reg = (await register(client)).json()
    _, challenge = pkce()
    authorized = await client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": reg["redirect_uris"][0],
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "pay",
    })
    consent_path = path_of(authorized.headers["location"])
    await sign_in(client, mailer, "bob@example.com", next=consent_path)
    page = await client.get(consent_path)
    assert page.status_code == 200
    assert "form-action 'self' https://claude.ai" in page.headers["content-security-policy"]
    req, csrf = form_value(page.text, "req"), form_value(page.text, "csrf")

    pending = oauth.read_pending(settings, req)
    assert pending is not None
    client.cookies.delete(oauth.binding_cookie_name(settings, pending.nonce))
    refused = await client.post("/oauth/consent", data={"req": req, "csrf": csrf, "action": "allow"})
    assert refused.status_code == 400 and "location" not in refused.headers
    assert "browser where you clicked Connect" in refused.text
    assert conn.execute("SELECT count(*) AS n FROM oauth_codes").fetchone()["n"] == 0

