"""Account navigation returns public layout without waiting for the remote database."""
from contextlib import contextmanager

import httpx
import pytest
from conftest import sign_in

from sentinel.web import auth, users
from sentinel.web.app import create_app

pytestmark = pytest.mark.anyio
PATHS = ["/account", "/account/payments", "/account/tasks", "/account/rules", "/account/books"]
USER_ID = "2f1c3d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


@pytest.mark.parametrize("path", PATHS)
async def test_shell_does_not_connect_to_database(settings, path):
    def unavailable():
        raise AssertionError("The loading shell must not wait for the database")

    app = create_app(settings, connect=unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.base_url) as client:
        client.cookies.set(auth.session_cookie_name(settings), auth.make_session(settings, USER_ID, 0))
        response = await client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 200
        assert 'id="account-loading"' in response.text
        assert 'aria-busy="true"' in response.text
        assert '/static/account-loading.js' in response.text
        assert 'full=1' in response.text
        assert 'connect-src \'self\'' in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert "user@example.com" not in response.text


@pytest.mark.parametrize("mode", ["fetch", "no-js", "csv"])
async def test_content_rechecks_live_session_and_never_returns_shell(settings, monkeypatch, mode):
    checked = []

    @contextmanager
    def connect():
        yield None

    def revoked(conn, user_id, epoch):
        checked.append(user_id)
        return None

    monkeypatch.setattr(users, "session_user", revoked)
    app = create_app(settings, connect=connect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.base_url) as client:
        client.cookies.set(auth.session_cookie_name(settings), auth.make_session(settings, USER_ID, 0))
        path = "/account/payments.csv" if mode == "csv" else "/account?full=1" if mode == "no-js" else "/account"
        headers = {"accept": "text/html"}
        if mode == "fetch":
            headers["x-sentinel-content"] = "1"
        response = await client.get(path, headers=headers)
        assert checked == [USER_ID]
        assert response.status_code == 303
        assert response.headers["location"].startswith("/signin")
        assert 'id="account-loading"' not in response.text


async def test_loading_shell_then_authenticated_content(browser, mailer):
    client = browser()
    await sign_in(client, mailer, "loading@example.com")
    shell = await client.get("/account?done=limits", headers={"accept": "text/html"})
    assert 'id="account-loading"' in shell.text
    assert "done=limits&amp;full=1" in shell.text
    content = await client.get("/account", headers={"accept": "text/html", "x-sentinel-content": "1"})
    assert content.status_code == 200
    assert 'id="account-loading"' not in content.text
    assert "loading@example.com" in content.text


@pytest.fixture
def section_client(settings, monkeypatch):
    """A live signed session with data sources replaced; no remote database or wallet calls."""
    @contextmanager
    def connect():
        yield None

    monkeypatch.setattr(users, "session_user", lambda *_: {"id": USER_ID, "email": "sections@example.com"})
    monkeypatch.setattr(users, "account_view", lambda *_, **__: {
        "email": "sections@example.com", "wallet_address": None, "agent": "my-agent", "agent_id": 7,
        "per_payment_cap": 1_000_000, "weekly_budget": 5_000_000, "week_spend": 0,
    })
    app = create_app(settings, connect=connect)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.base_url)
    client.cookies.set(auth.session_cookie_name(settings), auth.make_session(settings, USER_ID, 0))
    return client


async def test_overview_shell_has_connection_instructions_before_data(settings):
    def unavailable():
        raise AssertionError("The shell must not access the database")

    app = create_app(settings, connect=unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.base_url) as client:
        client.cookies.set(auth.session_cookie_name(settings), auth.make_session(settings, USER_ID, 0))
        response = await client.get("/account", headers={"accept": "text/html"})
        for section in ("identity", "overview_spending", "overview_setup", "overview_recent"):
            assert f'data-account-section="{section}"' in response.text
        assert settings.resource_url in response.text
        assert "claude mcp add" in response.text
        assert 'action="/account/signout-everywhere"' in response.text
        assert "No payments yet" not in response.text
        assert "Your wallet could not be created" not in response.text


async def test_identity_needs_no_account_queries(section_client, monkeypatch):
    def unavailable(*_):
        raise AssertionError("Identity must not wait for account data")

    monkeypatch.setattr(users, "account_view", unavailable)
    async with section_client as client:
        response = await client.get("/account", headers={"x-sentinel-section": "identity"})
    assert response.status_code == 200
    assert "sections@example.com" in response.text
    assert 'data-account-fragment="identity"' in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_limits_skip_sellers_and_wallet_lookups(section_client, monkeypatch):
    from sentinel import chain
    from sentinel.web import books

    def unavailable(*_):
        raise AssertionError("Limits must not wait for sellers or wallet services")

    monkeypatch.setattr(books, "list_sellers", unavailable)
    monkeypatch.setattr(chain, "cached_profile", unavailable)
    async with section_client as client:
        response = await client.get("/account/rules", headers={"x-sentinel-section": "rules_limits"})
    assert response.status_code == 200
    assert 'value="1.00"' in response.text
    assert 'action="/account/rules/limits"' in response.text
    assert "Approved sellers" not in response.text


async def test_sellers_render_before_wallet_checks(section_client, monkeypatch):
    from sentinel import chain
    from sentinel.web import books

    def unavailable(*_):
        raise AssertionError("Seller controls must not wait for wallet services")

    monkeypatch.setattr(chain, "cached_profile", unavailable)
    monkeypatch.setattr(books, "list_sellers", lambda *_: [{
        "name": "Example seller", "pay_to": "0x" + "a" * 40, "status": "approved", "source": "you",
        "own_status": "approved",
    }])
    async with section_client as client:
        response = await client.get("/account/rules", headers={"x-sentinel-section": "rules_sellers"})
    assert response.status_code == 200
    assert "Example seller" in response.text
    assert 'data-account-section="seller_wallet"' in response.text
    assert "Block</button>" in response.text
    assert "Approve this seller" in response.text


async def test_wallet_checks_only_apply_to_this_users_sellers(section_client, monkeypatch):
    from sentinel import chain
    from sentinel.web import books

    def unavailable(*_):
        raise AssertionError("An unlisted address must never trigger a wallet lookup")

    monkeypatch.setattr(chain, "cached_profile", unavailable)
    monkeypatch.setattr(books, "list_sellers", lambda *_: [])
    async with section_client as client:
        response = await client.get("/account/rules", headers={
            "x-sentinel-section": "seller_wallet", "x-sentinel-wallet": "0x" + "b" * 40,
        })
    assert response.status_code == 404


@pytest.mark.parametrize("section", ["identity", "overview_spending", "overview_recent"])
async def test_sections_recheck_revoked_sessions(section_client, monkeypatch, section):
    monkeypatch.setattr(users, "session_user", lambda *_: None)
    async with section_client as client:
        response = await client.get("/account", headers={"x-sentinel-section": section})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/signin")
    assert "sections@example.com" not in response.text


async def test_invalid_section_is_rejected(section_client):
    async with section_client as client:
        response = await client.get("/account", headers={"x-sentinel-section": "rules_limits"})
    assert response.status_code == 400


@pytest.mark.parametrize(("path", "section", "text"), [
    ("/account", "overview_spending", "Spent this week"),
    ("/account", "overview_cap", "Per payment cap"),
    ("/account", "overview_wallet", "Create my wallet"),
    ("/account", "overview_setup", "Get started"),
    ("/account", "overview_recent", "Nothing yet"),
    ("/account/payments", "payments_list", "No payments yet"),
    ("/account/tasks", "tasks_held", "Nothing is waiting for you"),
    ("/account/tasks", "tasks_list", "No tasks yet"),
    ("/account/books", "books_summary", "Books balance"),
    ("/account/books", "books_journal", "No entries yet"),
    ("/account/books", "books_totals", "Nothing booked yet"),
])
async def test_each_data_section_renders_independently(section_client, monkeypatch, path, section, text):
    from sentinel import link_approvals, mandates
    from sentinel.web import books

    for module, name in [(books, "recent_payments"), (books, "list_sellers"), (books, "payments_by_task"),
                         (books, "journal"), (mandates, "list_held"), (mandates, "list_for_agent")]:
        monkeypatch.setattr(module, name, lambda *_: [])
    monkeypatch.setattr(books, "has_live_grant", lambda *_: False)
    monkeypatch.setattr(books, "summary", lambda *_: {"spent": 0, "settled": 0})
    monkeypatch.setattr(books, "totals", lambda *_: {"balanced": True, "rows": []})
    monkeypatch.setattr(mandates, "count_pending", lambda *_: {"tasks": 0, "held": 0})
    monkeypatch.setattr(link_approvals, "expire", lambda *_: None)
    async with section_client as client:
        response = await client.get(path, headers={"x-sentinel-section": section})
    assert response.status_code == 200
    assert f'data-account-fragment="{section}"' in response.text
    assert text in response.text
    assert '<main' not in response.text


async def test_slow_history_does_not_hold_up_wallet(section_client, monkeypatch):
    import asyncio
    import threading

    from sentinel import mandates
    from sentinel.web import books

    started, release = threading.Event(), threading.Event()

    def slow_history(*_):
        started.set()
        assert release.wait(5), "The test did not release the history query"
        return []

    monkeypatch.setattr(books, "recent_payments", slow_history)
    monkeypatch.setattr(mandates, "count_pending", lambda *_: {"tasks": 0, "held": 0})
    async with section_client as client:
        history = asyncio.create_task(client.get("/account", headers={"x-sentinel-section": "overview_recent"}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            wallet = await asyncio.wait_for(
                client.get("/account", headers={"x-sentinel-section": "overview_wallet"}), timeout=2,
            )
            assert wallet.status_code == 200
            assert "Create my wallet" in wallet.text
            assert not history.done()
        finally:
            release.set()
            await history
