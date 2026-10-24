"""Tests for the signed in account pages over HTTP: Overview, Payments, Rules, Books and the payments CSV.

Everything is the whole app on the test connection (conftest fixtures browser, app, mailer, fake_wallets, pay_spy):
users sign in through the site, so each has their own fake wallet, agent and books. Covers the sign in redirect for
every page, the refusals every POST shares (foreign origin, missing or wrong CSRF, no session) with no side effects,
adding, blocking and re-approving sellers, limits that the guard enforces on the next pay, validation errors that
keep what was typed, isolation between two users, operator rows that stay untouchable, escaping of hostile text, the
CSV, balanced books, the dev only demo sellers, the getting started checklist and the whole journey over /mcp.
"""
import csv
import dataclasses
import io
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from conftest import ASSET, BROWSER_IP, DATACO, NETWORK, call_tool, connect_flow, form_value, sign_in
from fastmcp.utilities.asgi_transport import run_asgi_lifespan

from sentinel import payments
from sentinel.guard import Quote
from sentinel.web import app as web_app
from sentinel.web import books

COOKIE = "sentinel_session"
BOB_IP = "203.0.113.20"
EVIL = "https://evil.example"
SELLER = "0x1111111111111111111111111111111111111111"
OTHER_SELLER = "0x2222222222222222222222222222222222222222"
PAGES = [("/account", "Overview"), ("/account/payments", "Payments"), ("/account/rules", "Rules"),
         ("/account/books", "Books")]
REASON = "Need 2026 email open-rate benchmarks for the Q3 report the owner asked for; DataCo is named in the brief."


# Helpers


async def signed_in(browser, mailer, email="alice@example.com", ip=BROWSER_IP) -> httpx.AsyncClient:
    """A new browser signed in as this address (the wallet and books are made by the sign in)."""
    client = browser(ip)
    await sign_in(client, mailer, email)
    return client


def user_of(conn, client) -> dict[str, Any]:
    """The signed in user's row: id, agent_id, wallet_address, and the agent name."""
    user_id = client.cookies.get(COOKIE).split(".")[1]
    row = conn.execute(
        "SELECT u.id::text AS id, u.agent_id, u.wallet_address, a.name AS agent FROM users u"
        " LEFT JOIN agents a ON a.id = u.agent_id WHERE u.id = %s", (user_id,),
    ).fetchone()
    assert row is not None
    return row


async def csrf(client, path="/account") -> str:
    """The account CSRF token for this browser."""
    page = await client.get(path)
    assert page.status_code == 200, page.text
    return form_value(page.text, "csrf")


async def add_seller(client, name="NewCo", pay_to=SELLER, status="approved", token=None) -> httpx.Response:
    """POST the seller form the way the page does."""
    return await client.post("/account/rules/sellers", data={
        "csrf": token or await csrf(client, "/account/rules"), "name": name, "pay_to": pay_to, "status": status,
    })


def own_sellers(conn, user) -> list[dict[str, Any]]:
    """The user's personal seller rows."""
    return conn.execute(
        "SELECT name, pay_to, status, added_by FROM sellers WHERE agent_id = %s ORDER BY id", (user["agent_id"],)
    ).fetchall()


def clear_operator_list(conn) -> None:
    """Empty the operator's seller list for this test (rolled back with it), so nothing is approved to start with."""
    conn.execute("DELETE FROM sellers WHERE agent_id IS NULL")


def attempt(conn, agent, url="http://x/a", reason="why", pay_to=DATACO, amount=500_000) -> int | None:
    """Run the real guard for this agent; returns the approved attempt id, or None when it was blocked."""
    try:
        return payments.authorize(conn, agent, reason, Quote(url, pay_to, amount, ASSET, NETWORK, 300))
    except payments.PaymentRejected:
        return None


def rows_of(text) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@asynccontextmanager
async def serve(settings, **kw) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """A second app from these settings with its lifespan, and a browser factory for it."""
    application = web_app.create_app(settings, **kw)

    async def with_state(scope: Any, receive: Any, send: Any) -> None:
        await application({**scope, "state": {}}, receive, send)

    made: list[httpx.AsyncClient] = []

    def make(ip: str = BROWSER_IP) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, client=(ip, 50000)), base_url=settings.base_url,
            follow_redirects=False, headers={"origin": settings.base_url},
        )
        made.append(client)
        return client

    async with run_asgi_lifespan(with_state):
        yield make
    for client in made:
        await client.aclose()


# Pages and sign in


@pytest.mark.anyio
@pytest.mark.parametrize("path, next_path", [
    ("/account", "/account"), ("/account/payments", "/account/payments"), ("/account/rules", "/account/rules"),
    ("/account/books", "/account/books"), ("/account/payments.csv", "/account/payments"),
])
async def test_without_a_session_every_page_sends_you_to_sign_in_and_back(browser, path, next_path):
    response = await browser().get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin?" + urlencode({"next": next_path})


@pytest.mark.anyio
async def test_signing_in_with_next_comes_back_to_that_account_page(browser, mailer):
    client = browser()
    response = await sign_in(client, mailer, "alice@example.com", next="/account/rules")
    assert response.headers["location"] == "/account/rules"
    assert (await client.get("/account/rules")).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("path, label", PAGES)
async def test_each_page_renders_with_its_tab_marked_and_the_site_headers(browser, mailer, path, label):
    client = await signed_in(browser, mailer)
    response = await client.get(path)
    assert response.status_code == 200
    assert "alice@example.com" in response.text
    current = re.findall(r'<a href="([^"]+)" aria-current="page"[^>]*>([^<]+)</a>', response.text)
    assert current == [(path, label)]
    assert response.headers["cache-control"] == "no-store"
    assert "script-src" in response.headers["content-security-policy"]
    assert "<script>" not in response.text.replace('<script src="https://cdn.jsdelivr.net', "")  # no script of ours


# Refusals shared by every POST


FORMS = [
    ("/account/rules/limits", {"cap": "1", "budget": "2"}),
    ("/account/rules/sellers", {"name": "NewCo", "pay_to": SELLER, "status": "approved"}),
]


async def snapshot(conn, user):
    limits = conn.execute("SELECT per_payment_cap, weekly_budget FROM agents WHERE id = %s",
                          (user["agent_id"],)).fetchone()
    return limits, own_sellers(conn, user)


@pytest.mark.anyio
@pytest.mark.parametrize("path, data", FORMS)
@pytest.mark.parametrize("case", ["foreign origin", "no csrf", "wrong csrf", "no session"])
async def test_every_post_refuses_what_it_should_with_no_side_effects(browser, mailer, conn, path, data, case):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    token = await csrf(client)
    before = await snapshot(conn, user)
    if case == "foreign origin":
        response = await client.post(path, data={**data, "csrf": token}, headers={"origin": EVIL})
        assert response.status_code == 403
    elif case == "no csrf":
        assert (await client.post(path, data=data)).status_code == 403
    elif case == "wrong csrf":
        assert (await client.post(path, data={**data, "csrf": token[::-1]})).status_code == 403
    else:
        stranger = browser(BOB_IP)
        response = await stranger.post(path, data={**data, "csrf": token})
        assert response.status_code == 303
        assert response.headers["location"] == "/signin?" + urlencode({"next": "/account/rules"})
    assert await snapshot(conn, user) == before


# Sellers


@pytest.mark.anyio
async def test_adding_a_seller_saves_a_personal_row_and_shows_the_saved_banner(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    response = await add_seller(client, "  NewCo ")
    assert response.status_code == 303 and response.headers["location"] == "/account/rules?done=seller"
    assert [(r["name"], r["pay_to"], r["status"], r["added_by"]) for r in own_sellers(conn, user)] == [
        ("NewCo", SELLER, "approved", "user")]
    page = await client.get("/account/rules?done=seller")
    assert 'role="status"' in page.text and "Saved." in page.text and "NewCo" in page.text and SELLER in page.text
    assert "Saved." not in (await client.get("/account/rules")).text  # the banner is for that redirect only


@pytest.mark.anyio
async def test_blocking_and_reapproving_a_seller_toggles_the_users_own_row(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await add_seller(client)
    assert (await client.get("/account/rules")).text.count('name="status" value="blocked"') == 1  # the Block button
    await add_seller(client, status="blocked")
    rows = own_sellers(conn, user)
    assert len(rows) == 1 and rows[0]["status"] == "blocked"
    assert (await client.get("/account/rules")).text.count('name="status" value="approved"') >= 1  # Approve toggle
    await add_seller(client, status="approved")
    assert [r["status"] for r in own_sellers(conn, user)] == ["approved"]


@pytest.mark.anyio
@pytest.mark.parametrize("name, pay_to, status, message", [
    ("NewCo", "0x123", "approved", "not a wallet address"),
    ("   ", SELLER, "approved", "needs a name"),
    ("x" * 101, SELLER, "approved", "at most 100"),
    ("NewCo", SELLER, "pending", "approved or blocked"),
    ("NewCo", "OWN", "approved", "your own wallet"),
])
async def test_a_bad_seller_is_a_400_with_the_message_and_what_was_typed_and_changes_nothing(
    browser, mailer, conn, name, pay_to, status, message,
):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    response = await add_seller(client, name, user["wallet_address"] if pay_to == "OWN" else pay_to, status)
    assert response.status_code == 400
    assert message in response.text and 'role="alert"' in response.text
    assert (name.strip() == "" or name[:100] in response.text)
    assert own_sellers(conn, user) == []


@pytest.mark.anyio
async def test_a_hostile_done_value_is_ignored(browser, mailer):
    client = await signed_in(browser, mailer)
    for value in ["<script>alert(1)</script>", "other", "limits%00"]:
        page = await client.get("/account/rules", params={"done": value})
        assert page.status_code == 200 and 'role="status"' not in page.text and "alert(1)" not in page.text


@pytest.mark.anyio
async def test_operator_rows_cannot_be_changed_a_post_makes_a_personal_row_beside_them(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    operator = lambda: conn.execute(  # noqa: E731
        "SELECT name, status, added_by FROM sellers WHERE agent_id IS NULL AND pay_to = %s", (DATACO,)).fetchall()
    before = operator()
    page = await client.get("/account/rules")
    assert DATACO in page.text and "Operator" in page.text
    assert f'name="pay_to" value="{DATACO}"' not in page.text  # no buttons on an operator row
    await add_seller(client, "Mine", DATACO, "blocked")
    assert operator() == before
    assert [(r["name"], r["status"]) for r in own_sellers(conn, user)] == [("Mine", "blocked")]
    after = await client.get("/account/rules")
    assert after.text.count(DATACO) >= 2  # shown once, now as the user's own blocked row
    assert before == [{"name": "DataCo", "status": "approved", "added_by": "human"}]


# Limits


@pytest.mark.anyio
async def test_saving_limits_updates_the_users_own_agent_and_the_banner_shows(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    page = await client.get("/account/rules")
    assert 'value="10.00"' in page.text and 'value="50.00"' in page.text
    response = await client.post("/account/rules/limits", data={
        "csrf": form_value(page.text, "csrf"), "cap": "$2.50", "budget": "20"})
    assert response.status_code == 303 and response.headers["location"] == "/account/rules?done=limits"
    row = conn.execute(
        "SELECT per_payment_cap, weekly_budget FROM agents WHERE id = %s", (user["agent_id"],)
    ).fetchone()
    assert (str(row["per_payment_cap"]), str(row["weekly_budget"])) == ("2.500000", "20.000000")
    saved = await client.get("/account/rules?done=limits")
    assert "Saved." in saved.text and 'value="2.50"' in saved.text and 'value="20.00"' in saved.text


@pytest.mark.anyio
async def test_a_user_cannot_change_another_agents_limits_even_by_naming_it(browser, mailer, conn):
    alice = await signed_in(browser, mailer)
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    token = await csrf(alice)
    await alice.post("/account/rules/limits", data={
        "csrf": token, "cap": "1", "budget": "2", "agent": user_of(conn, bob)["agent"], "agent_id": "1"})
    assert str(conn.execute("SELECT per_payment_cap FROM agents WHERE name = %s",
                            (user_of(conn, bob)["agent"],)).fetchone()["per_payment_cap"]) == "10.000000"
    assert str(conn.execute("SELECT per_payment_cap FROM agents WHERE name = 'research-agent'").fetchone()[
        "per_payment_cap"]) == "10.000000"


@pytest.mark.anyio
@pytest.mark.parametrize("cap, budget, message", [
    ("60", "50", "cannot be more than the weekly budget"),
    ("abc", "50", "is not a dollar amount"),
    ("5", "", "is not a dollar amount"),
    ("0", "10", "must be more than $0"),
    ("1.1234567", "10", "more than 6 decimals"),
])
async def test_bad_limits_are_a_400_with_the_message_and_the_typed_values(browser, mailer, conn, cap, budget, message):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    before = await snapshot(conn, user)
    response = await client.post("/account/rules/limits", data={
        "csrf": await csrf(client, "/account/rules"), "cap": cap, "budget": budget})
    assert response.status_code == 400 and message in response.text
    if cap:
        assert f'value="{cap}"' in response.text
    assert await snapshot(conn, user) == before


# Isolation between users


@pytest.mark.anyio
async def test_one_user_never_sees_or_changes_another_users_sellers_payments_books_or_csv(
    browser, mailer, conn, pay_spy,
):
    alice = await signed_in(browser, mailer)
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    a, b = user_of(conn, alice), user_of(conn, bob)
    await add_seller(alice, "AliceOnlyCo", SELLER)
    await add_seller(bob, "BobOnlyCo", OTHER_SELLER)
    await pay_spy("http://x/alice-secret-url", None, agent=a["agent"], reason="alice-secret-reason")
    await pay_spy("http://x/bob-secret-url", None, agent=b["agent"], reason="bob-secret-reason")

    for client, mine, theirs in [(alice, "alice", "bob"), (bob, "bob", "alice")]:
        text = {path: (await client.get(path)).text for path, _ in PAGES}
        csv_text = (await client.get("/account/payments.csv")).text
        everything = "".join(text.values()) + csv_text
        assert f"{mine}-secret-url" in everything and f"{theirs}-secret-url" not in everything
        assert f"{theirs}-secret-reason" not in everything
        assert f"{theirs.capitalize()}OnlyCo" not in everything
    assert SELLER not in (await bob.get("/account/rules")).text
    # The same address can be approved by both users, as two separate rows.
    await add_seller(bob, "AlsoAlice", SELLER)
    assert len(own_sellers(conn, a)) == 1 and len(own_sellers(conn, b)) == 2


# Payments


@pytest.mark.anyio
async def test_the_payments_page_lists_statuses_why_and_a_transaction_link(browser, mailer, conn, pay_spy):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await pay_spy("http://x/paid", None, agent=user["agent"], reason="a real purchase")
    attempt(conn, user["agent"], "http://x/scam", "from a page", pay_to=OTHER_SELLER)
    attempt(conn, user["agent"], "http://x/inflight", "in flight")
    page = (await client.get("/account/payments")).text
    for text in ["SETTLED", "BLOCKED", "PROCESSING", "not approved", "$0.50", "a real purchase", "http://x/paid"]:
        assert text in page, text
    assert "Seller not approved" in page and "is not on the owner" in page  # the label and the guard's sentence
    assert "In progress" in page and "<details" not in page and "SELLER" not in page
    assert "up to the latest 20,000" in page
    assert 'href="https://sepolia.basescan.org/tx/0xtx' in page
    assert "Download CSV" in page and "/account/payments.csv" in page


@pytest.mark.anyio
async def test_a_user_with_no_payments_gets_a_helpful_empty_state(browser, mailer):
    client = await signed_in(browser, mailer)
    page = (await client.get("/account/payments")).text
    assert "No payments yet" in page and 'href="/account/rules"' in page and "Download CSV" not in page


@pytest.mark.anyio
async def test_hostile_reasons_urls_and_seller_names_are_escaped_on_every_page(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await add_seller(client, "<script>alert('name')</script>", DATACO.upper().replace("0X", "0x"))
    attempt(conn, user["agent"], "http://x/<script>alert('url')</script>", "<script>alert('reason')</script>")
    for path in ["/account", "/account/payments", "/account/rules", "/account/books"]:
        text = (await client.get(path)).text
        assert "<script>alert(" not in text, path
    payments_page = (await client.get("/account/payments")).text
    for what in ["name", "url", "reason"]:
        assert f"&lt;script&gt;alert(&#39;{what}&#39;)&lt;/script&gt;" in payments_page, what


@pytest.mark.anyio
async def test_the_csv_defuses_formulas_holds_only_the_users_rows_and_downloads_as_a_file(browser, mailer, conn):
    alice = await signed_in(browser, mailer)
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    attempt(conn, user_of(conn, alice)["agent"], "http://x/a", "=HYPERLINK(\"http://evil\",\"x\")")
    attempt(conn, user_of(conn, bob)["agent"], "http://x/b", "bobs row")
    response = await alice.get("/account/payments.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert re.fullmatch(r'attachment; filename="sentinel-payments-\d{4}-\d{2}-\d{2}\.csv"',
                        response.headers["content-disposition"])
    rows = rows_of(response.text)
    assert len(rows) == 2 and rows[0][0] == "id"
    reason = rows[1][rows[0].index("reason")]
    assert reason.startswith("'=HYPERLINK") and "bobs row" not in response.text
    assert rows[1][rows[0].index("agent")] == user_of(conn, alice)["agent"]


@pytest.mark.anyio
async def test_the_csv_holds_at_most_csv_limit_rows(browser, mailer, conn, monkeypatch):
    assert books.CSV_LIMIT == 20_000
    monkeypatch.setattr(books, "CSV_LIMIT", 2)
    client = await signed_in(browser, mailer)
    agent = user_of(conn, client)["agent"]
    for i in range(3):
        attempt(conn, agent, f"http://x/{i}", "r")
    assert len(rows_of((await client.get("/account/payments.csv")).text)) == 3  # header plus the newest two
    assert "up to the latest 2." in (await client.get("/account/payments")).text


@pytest.mark.anyio
async def test_the_pages_hold_to_the_screen_width_no_wide_text_and_tables_scroll_inside_wrappers(
    browser, mailer, conn, pay_spy,
):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await pay_spy("http://x/paid", None, agent=user["agent"], reason="r")
    for path in ["/account", "/account/payments", "/account/rules", "/account/books"]:
        text = (await client.get(path)).text
        assert "min-w-0 max-w-[9rem] truncate" in text and "shrink-0" in text, path  # email truncates, Sign out stays
        main = re.search(r'<main class="([^"]*)"', text)
        assert main and "max-w-5xl" in main.group(1).split() and "min-w-0" in main.group(1).split(), path
        # max-w-full next to max-w-5xl wins in CSS and stretches the page edge to edge on a desktop (a real regression)
        assert "max-w-full" not in main.group(1).split(), path
        for table in re.finditer(r"<table", text):
            before = text[max(0, table.start() - 120):table.start()]
            assert "overflow-x-auto" in before, (path, before)
    assert "grid-cols-1" in (await client.get("/account")).text


@pytest.mark.anyio
async def test_a_seller_over_the_cap_is_a_400_banner_but_an_existing_one_can_still_change(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    for i in range(books.SELLER_LIMIT):
        conn.execute(
            "INSERT INTO sellers (name, pay_to, network, asset, status, added_by, agent_id)"
            " VALUES (%s, %s, %s, %s, 'approved', 'user', %s)",
            (f"S{i}", f"0x{i + 1:040x}", NETWORK, ASSET, user["agent_id"]),
        )
    response = await add_seller(client, "OneTooMany", SELLER)
    assert response.status_code == 400 and "up to 200 sellers" in response.text
    assert len(own_sellers(conn, user)) == books.SELLER_LIMIT
    assert (await add_seller(client, "Renamed", f"0x{1:040x}", "blocked")).status_code == 303


# Books


@pytest.mark.anyio
async def test_the_books_page_shows_balanced_scoped_books(browser, mailer, conn, pay_spy):
    alice = await signed_in(browser, mailer)
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    a, b = user_of(conn, alice), user_of(conn, bob)
    await pay_spy("http://x/alice-book", None, agent=a["agent"], reason="r")
    pay_spy.amount = 250_000
    await pay_spy("http://x/alice-book-two", None, agent=a["agent"], reason="r")
    await pay_spy("http://x/bob-book", None, agent=b["agent"], reason="r")
    page = (await alice.get("/account/books")).text
    assert "Books balance" in page and "Out of balance" not in page
    assert "$0.75" in page and "alice-book-two" in page and "bob-book" not in page
    assert f"1010-{a['agent_id']}" in page and f"1010-{b['agent_id']}" not in page
    assert "Dr" in page and "Cr" in page and "Totals by account" in page and "not booked yet" in page
    assert "/account/payments.csv" in page
    assert 'Paid DataCo for <span class="break-all">x/alice-book-two</span>' in page
    assert 'title="x402 payment http://x/alice-book-two tx 0xtx' in page
    assert 'href="https://sepolia.basescan.org/tx/0xtx' in page and "View payments" in page
    assert "USDC Cash 0x" in page and "..." in page and not re.search(r"USDC Cash 0x[0-9a-fA-F]{40}", page)
    empty = await signed_in(browser, mailer, "carol@example.com", "203.0.113.30")
    assert "No entries yet" in (await empty.get("/account/books")).text


# Demo sellers (dev mode only)


@pytest.mark.anyio
async def test_demo_sellers_show_in_dev_mode_without_the_scammer_and_approving_one_works(
    browser, mailer, conn, monkeypatch,
):
    scam = "0x" + "5" * 40
    monkeypatch.setenv("SELLER_A_ADDRESS", SELLER)
    monkeypatch.setenv("SELLER_B_ADDRESS", OTHER_SELLER)
    monkeypatch.setenv("SELLER_C_ADDRESS", scam)
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    page = (await client.get("/account/rules")).text
    assert "Demo sellers" in page and "InsightLab" in page and SELLER in page and OTHER_SELLER in page
    assert scam not in page
    token = form_value(page, "csrf")
    response = await client.post("/account/rules/sellers", data={
        "csrf": token, "name": "InsightLab", "pay_to": OTHER_SELLER, "status": "approved"})
    assert response.status_code == 303
    assert [(r["name"], r["pay_to"]) for r in own_sellers(conn, user)] == [("InsightLab", OTHER_SELLER)]
    after = (await client.get("/account/rules")).text
    card = after.split("Demo sellers")[1]  # InsightLab is approved now, so only the other demo seller is offered
    assert OTHER_SELLER not in card and SELLER in card and OTHER_SELLER in after.split("Demo sellers")[0]


@pytest.mark.anyio
async def test_demo_sellers_never_show_outside_dev_mode(
    settings, connect, mailer, fake_wallets, pay_spy, monkeypatch,
):
    monkeypatch.setenv("SELLER_A_ADDRESS", SELLER)
    production = dataclasses.replace(settings, dev_mode=False)
    async with serve(production, connect=connect, mailer=mailer, pay_fn=pay_spy) as make:
        client = make()
        await sign_in(client, mailer, "alice@example.com")
        page = (await client.get("/account/rules")).text
        assert "Demo sellers" not in page and SELLER not in page


# Overview


@pytest.mark.anyio
async def test_the_overview_shows_the_stats_the_wallet_and_the_connect_card(browser, mailer, conn, pay_spy):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await pay_spy("http://x/p", None, agent=user["agent"], reason="r")
    page = (await client.get("/account")).text
    assert "Spent this week" in page and "$0.50" in page and "of $50.00" in page and "Per payment cap" in page
    assert 'style="width: 1%"' in page and 'role="progressbar"' in page and "bg-stone-900" in page
    assert f"https://sepolia.basescan.org/address/{user['wallet_address']}" in page
    assert 'href="https://faucet.circle.com"' in page and "Fund it" in page and "Base Sepolia" in page
    assert "claude mcp add --transport http sentinel http://localhost:8000/mcp" in page and "<details" in page
    assert "Recent payments" in page and "SETTLED" in page
    assert "Sign out everywhere" in page


@pytest.mark.anyio
@pytest.mark.parametrize("spent, color", [(39_900_000, "bg-stone-900"), (40_000_000, "bg-amber-500"),
                                         (50_000_000, "bg-red-600"), (60_000_000, "bg-red-600")])
async def test_the_budget_bar_turns_amber_from_80_percent_and_red_at_100(browser, mailer, conn, spent, color):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    conn.execute("UPDATE agents SET per_payment_cap = 100, weekly_budget = 50 WHERE id = %s", (user["agent_id"],))
    conn.execute(
        "INSERT INTO payment_attempts (agent_id, url, pay_to, amount, status)"
        " VALUES (%s, 'http://x', %s, %s, 'settled')",
        (user["agent_id"], DATACO, spent / 1_000_000),
    )
    page = (await client.get("/account")).text
    bar = re.search(r'<div class="h-full rounded-full ([^"]+)" style="width: (\d+)%">', page)
    assert bar and bar.group(1) == color and int(bar.group(2)) == min(100, spent // 500_000)


@pytest.mark.anyio
async def test_the_checklist_ticks_as_steps_complete_then_says_all_set(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    clear_operator_list(conn)
    page = (await client.get("/account")).text
    assert "Get started" in page and page.count("(done)") == 1 and page.count("(to do)") == 2
    assert 'href="/account/rules"' in page
    await add_seller(client)
    page = (await client.get("/account")).text
    assert page.count("(done)") == 2 and page.count("(to do)") == 1
    await connect_flow(client, mailer, "alice@example.com")
    page = (await client.get("/account")).text
    assert "All set" in page and "Get started" not in page


@pytest.mark.anyio
async def test_an_operator_approved_seller_counts_for_the_checklist(browser, mailer):
    client = await signed_in(browser, mailer)  # the test schema lists DataCo for the operator
    assert (await client.get("/account")).text.count("(done)") == 2


@pytest.mark.anyio
async def test_has_live_grant_follows_a_real_connection_and_sign_out_everywhere(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    assert not books.has_live_grant(conn, user["id"])
    await connect_flow(client, mailer, "alice@example.com")
    assert books.has_live_grant(conn, user["id"])
    await client.post("/account/signout-everywhere", data={"csrf": await csrf(client)})
    assert not books.has_live_grant(conn, user["id"])


# No wallet yet


@pytest.mark.anyio
async def test_without_a_wallet_other_pages_point_to_overview_and_posts_change_nothing(
    browser, mailer, conn, fake_wallets,
):
    fake_wallets.fail = True
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    assert 'action="/account/wallet"' in (await client.get("/account")).text
    for path in ["/account/payments", "/account/rules", "/account/books"]:
        page = (await client.get(path)).text
        assert "Your wallet is not ready yet" in page and 'href="/account"' in page, path
    token = await csrf(client)
    for path, data in FORMS:
        response = await client.post(path, data={**data, "csrf": token})
        assert response.status_code == 400 and "Create your wallet first" in response.text
    assert conn.execute("SELECT count(*) AS n FROM sellers WHERE agent_id IS NOT NULL").fetchone()["n"] == 0
    csv_response = await client.get("/account/payments.csv")
    assert csv_response.status_code == 303 and csv_response.headers["location"] == "/account/payments"


# The whole journey


@pytest.mark.anyio
async def test_sign_in_approve_a_seller_on_the_rules_page_then_pay_over_mcp(browser, mailer, conn, pay_spy):
    client = browser()
    tokens = await connect_flow(client, mailer, "alice@example.com")  # signs in, creates the wallet, connects
    user = user_of(conn, client)
    clear_operator_list(conn)
    url = "http://seller.test/benchmarks/email"

    blocked = await call_tool(client, tokens.access_token, "pay", {"url": url, "reason": REASON})
    assert blocked["status"] == "BLOCKED" and blocked["rule"] == "seller"
    assert (await client.get("/account/payments")).text.count("BLOCKED") >= 1

    await add_seller(client, "DataCo", DATACO)
    settled = await call_tool(client, tokens.access_token, "pay", {"url": url + "?again=1", "reason": REASON})
    assert settled["status"] == "SETTLED" and len(pay_spy.calls) == 2
    assert pay_spy.calls[-1][2] == user["agent"]
    page = (await client.get("/account/payments")).text
    assert "SETTLED" in page and "DataCo" in page
    assert "Books balance" in (await client.get("/account/books")).text

    await client.post("/account/rules/limits", data={
        "csrf": await csrf(client, "/account/rules"), "cap": "0.25", "budget": "5"})
    capped = await call_tool(client, tokens.access_token, "pay", {"url": url + "?third=1", "reason": REASON})
    assert capped["status"] == "BLOCKED" and capped["rule"] == "cap"

    await add_seller(client, "DataCo", DATACO, "blocked")
    stopped = await call_tool(client, tokens.access_token, "pay", {"url": url + "?fourth=1", "reason": REASON})
    assert stopped["status"] == "BLOCKED" and stopped["rule"] == "seller"
