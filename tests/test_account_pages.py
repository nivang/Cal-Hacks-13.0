"""Tests for the signed in account pages over HTTP: Overview, Payments, Rules, Books and the payments CSV.

Everything is the whole app on the test connection (conftest fixtures browser, app, mailer, fake_wallets, pay_spy):
users sign in through the site, so each has their own fake wallet, agent and books. Covers the sign in redirect for
every page, the refusals every POST shares (foreign origin, missing or wrong CSRF, no session) with no side effects,
adding, blocking and re-approving sellers, limits that the guard enforces on the next pay, validation errors that
keep what was typed, isolation between two users, operator rows that stay untouchable, escaping of hostile text, the
CSV, balanced books, the dev only demo sellers, the getting started checklist and the whole journey over /mcp.
"""
import csv
import io
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from conftest import ASSET, BROWSER_IP, DATACO, NETWORK, call_tool, connect_flow, form_value, sign_in
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from markupsafe import escape

from sentinel import db, mandates, payments
from sentinel.guard import Quote
from sentinel.web import app as web_app
from sentinel.web import books, pages

COOKIE = "sentinel_session"
BOB_IP = "203.0.113.20"
EVIL = "https://evil.example"
SELLER = "0x1111111111111111111111111111111111111111"
OTHER_SELLER = "0x2222222222222222222222222222222222222222"
PAGES = [("/account", "Overview"), ("/account/live", "Live"), ("/account/payments", "Payments"),
         ("/account/tasks", "Tasks"), ("/account/rules", "Settings"), ("/account/books", "Books")]
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
    ("/account/tasks", "/account/tasks"), ("/account/live", "/account/live"),
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
    links = re.findall(r'<a href="([^"]+)" aria-current="page"[^>]*>(.*?)</a>', response.text, re.DOTALL)
    current = [(href, re.sub(r"<[^>]+>", "", content).strip()) for href, content in links]
    assert current == [(path, label)]
    assert response.headers["cache-control"] == "no-store"
    assert "script-src" in response.headers["content-security-policy"]
    assert "<script>" not in response.text.replace('<script src="https://cdn.jsdelivr.net', "")  # no script of ours


# Refusals shared by every POST


FORMS = [
    ("/account/rules/limits", {"cap": "1", "budget": "2"}),
    ("/account/rules/sellers", {"name": "NewCo", "pay_to": SELLER, "status": "approved"}),
    ("/account/rules/auto-buy", {"auto_buy": "on"}),
]


async def snapshot(conn, user):
    limits = conn.execute("SELECT per_payment_cap, weekly_budget, auto_buy_best_value FROM agents WHERE id = %s",
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
    # an unlisted seller is judged by the wallet model: the label and the guard's sentence
    assert any(str(escape(pages.RULE_LABELS[rule])) in page for rule in ("wallet_risk", "wallet_unknown"))
    assert "not on your approved list" in page
    assert "In progress" in page and "Purchase details" in page and "<details" in page
    assert "up to the latest 20,000" in page
    assert 'href="https://sepolia.basescan.org/tx/0xtx' in page
    assert "Download CSV" in page and "/account/payments.csv" in page


@pytest.mark.anyio
async def test_the_live_page_shows_each_payment_step_by_step(browser, mailer, conn, pay_spy):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    task = task_for(conn, user, "Brief me")
    paid = in_task(conn, user, task, "settled", url="http://x/paid")
    conn.execute("UPDATE payment_attempts SET reason = 'a real purchase', tx_hash = '0xtx' WHERE id = %s", (paid,))
    in_task(conn, user, task, "blocked", url="http://x/scam")
    held_for(conn, user, task)
    page = (await client.get("/account/live")).text
    assert f"Task {task}" in page and "Write the Q3 email report." in page
    for text in ["Every request, as it happens", "Your agent", "processed", "pays if safe", "Policy",
                 "Seller", "Task check", "a real purchase",
                 'href="https://sepolia.basescan.org/tx/0xtx']:
        assert text in page, text
    assert page.count('<li class="lv-card"') == 3
    assert 'lv-out is-ok' in page and 'lv-out is-stop' in page and 'lv-out is-wait' not in page
    for gone in ["waiting for you", "Waiting for you", "Held for you", "/account/approvals/", ">Decide<",
                 "Needs your confirmation"]:
        assert gone not in page, gone


def in_task(conn, user, task_id, status, amount=500_000, preview=False, ml=None, url="http://x/a") -> int:
    """One attempt of this user's agent under this task with this status (and model score), straight in the table."""
    quote = {"url": url, "pay_to": DATACO, "amount": amount, "asset": ASSET, "network": NETWORK, "timeout_s": 300}
    model, score, threshold = ml or (None, None, None)
    return db.insert(conn, "INSERT INTO payment_attempts (agent_id, url, reason, pay_to, amount, quote_json, status,"
                     " decision, rule_fired, mandate_id, preview, ml_model, ml_score, ml_threshold)"
                     " VALUES (%s, %s, 'why', %s, %s, %s::jsonb, %s, 'BLOCK', %s, %s, %s, %s, %s, %s)",
                     (user["agent_id"], url, DATACO, db.usdc(amount), json.dumps(quote), status,
                      "injection_ml: Seller text tells the agent what to do." if model == "injection" else
                      "seller: Not listed.", task_id, preview, model, score, threshold))


@pytest.mark.anyio
async def test_live_counts_every_request_and_everything_sentinel_stopped(browser, mailer, conn):
    user = user_of(conn, await signed_in(browser, mailer))
    task = task_for(conn, user)
    for status, amount, preview in [("settled", 100_000, False), ("blocked", 200_000, False),
                                    ("escalated", 300_000, False), ("voided", 400_000, False),
                                    ("denied", 500_000, False), ("blocked", 600_000, True),
                                    ("escrowed", 700_000, False), ("failed", 800_000, False)]:
        in_task(conn, user, task, status, amount, preview)
    counts = books.live_view(conn, user["agent_id"])["counts"]
    assert counts["requests"] == 8  # every attempt, previews included
    assert counts["stopped"] == 5  # blocked, escalated, voided, denied and the preview it turned away
    assert counts["kept_back"] == 200_000 + 300_000 + 400_000 + 500_000 + 600_000
    assert counts["paid"] == 1 and counts["paid_amount"] == 100_000 and counts["escrowed"] == 1
    assert "held" not in counts


@pytest.mark.anyio
async def test_the_model_score_is_shown_wherever_a_payment_is(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    task = task_for(conn, user)
    in_task(conn, user, task, "blocked", ml=("injection", 0.97, 0.42))
    in_task(conn, user, task, "settled", ml=("wallet_risk", 0.12, 0.5), url="http://x/b")
    for path in ["/account/live", "/account/payments", "/account"]:
        page = (await client.get(path)).text
        assert "Injection model 0.97 · blocks at 0.42" in page, path
        assert "Wallet risk model 0.12 · escrow only at 0.50" in page, path
    live = (await client.get("/account/live")).text
    assert 'lv-ml is-hit">Injection model 0.97' in live and 'lv-ml">Wallet risk model 0.12' in live


@pytest.mark.anyio
async def test_the_live_tag_changes_when_a_payment_moves_and_stays_put_otherwise(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    first = books.fingerprint(conn, user["agent_id"])
    assert books.fingerprint(conn, user["agent_id"]) == first
    attempt_id = in_task(conn, user, None, "approved")
    second = books.fingerprint(conn, user["agent_id"])
    assert second != first
    conn.execute("UPDATE payment_attempts SET status = 'failed' WHERE id = %s", (attempt_id,))
    assert books.fingerprint(conn, user["agent_id"]) != second


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
    stylesheet = (await client.get("/static/site.css")).text
    email_styles = re.search(r"\.sidebar-email\s*\{([^}]+)\}", stylesheet)
    assert email_styles and all(rule in email_styles.group(1) for rule in (
        "min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap",
    ))
    main_styles = re.search(r"\.account-main\s*\{([^}]+)\}", stylesheet)
    assert main_styles and "max-width:1120px" in main_styles.group(1)
    for path in ["/account", "/account/payments", "/account/rules", "/account/books"]:
        text = (await client.get(path)).text
        assert 'class="sidebar-email"' in text and 'class="sidebar-signout"' in text, path
        main = re.search(r'<main\b[^>]*class="([^"]*)"', text)
        assert main and {"account-main", "min-w-0"} <= set(main.group(1).split()), path
        assert "max-w-full" not in main.group(1).split(), path
        for table in re.finditer(r"<table", text):
            before = text[max(0, table.start() - 120):table.start()]
            assert "overflow-x-auto" in before, (path, before)


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


# Overview


@pytest.mark.anyio
async def test_the_overview_shows_stats_and_wallet_with_setup_controls_in_settings(browser, mailer, conn, pay_spy):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    await pay_spy("http://x/p", None, agent=user["agent"], reason="r")
    page = (await client.get("/account")).text
    assert "Spent this week" in page and "$0.50" in page and "of $50.00" in page and "per payment cap" in page
    assert 'style="width: 1%"' in page and 'role="progressbar"' in page and "bg-stone-900" in page
    assert f"https://sepolia.basescan.org/address/{user['wallet_address']}" in page
    assert 'href="https://faucet.circle.com"' in page and "Fund it" in page and "Base Sepolia" in page
    settings_page = (await client.get("/account/rules")).text
    assert "claude mcp add --transport http sentinel http://localhost:8000/mcp" in settings_page
    assert "<details" in settings_page
    assert "Recent payments" in page and "SETTLED" in page
    assert "Sign out everywhere" in settings_page


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
    assert "Finish setup" in page and page.count("(done)") == 1 and page.count("(to do)") == 2
    assert 'href="/account/rules"' in page
    await add_seller(client)
    page = (await client.get("/account")).text
    assert page.count("(done)") == 2 and page.count("(to do)") == 1
    await connect_flow(client, mailer, "alice@example.com")
    page = (await client.get("/account")).text
    assert "All set" in page and "Finish setup" not in page


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

    first = await call_tool(client, tokens.access_token, "pay", {"url": url, "reason": REASON})
    assert first["status"] in ("SETTLED", "BLOCKED")  # Sentinel decides an unlisted seller itself, nobody is asked
    page = (await client.get("/account/payments")).text
    assert "AWAITING APPROVAL" not in page and "/account/approvals/" not in page
    calls = len(pay_spy.calls)

    await add_seller(client, "DataCo", DATACO)
    settled = await call_tool(client, tokens.access_token, "pay", {"url": url + "?again=1", "reason": REASON})
    assert settled["status"] == "SETTLED" and len(pay_spy.calls) == calls + 1
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


# Tasks


def task_for(conn, user, text="Write the Q3 email report; buy benchmarks from DataCo.") -> int:
    """A task for this user's agent, as start_task stores it."""
    return mandates.create(conn, user["agent_id"], text, "Write the Q3 email report.", ["email benchmarks"],
                           ["crypto signals"], 50_000_000, None)


def held_for(conn, user, task_id, url="http://seller.test/benchmarks/email") -> int:
    """A held (escalated) payment under this task."""
    quote = {"url": url, "pay_to": DATACO, "amount": 500_000, "asset": ASSET, "network": NETWORK, "timeout_s": 300}
    return db.insert(conn, "INSERT INTO payment_attempts (agent_id, url, reason, pay_to, amount, quote_json, status,"
                     " decision, rule_fired, mandate_id, intent_tier) VALUES (%s, %s, 'why', %s, 0.5, %s::jsonb,"
                     " 'escalated', 'ESCALATE', 'judge: The owner should decide this one.', %s, 2)",
                     (user["agent_id"], url, DATACO, json.dumps(quote), task_id))


def task_status(conn, task_id) -> tuple[str, int]:
    row = conn.execute("SELECT status, (budget * 1000000)::bigint AS budget FROM mandates WHERE id = %s",
                       (task_id,)).fetchone()
    return row["status"], row["budget"]


@pytest.mark.anyio
async def test_the_tasks_page_shows_what_the_owner_asked_and_nothing_to_confirm(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    task_for(conn, user)
    page = (await client.get("/account/tasks")).text
    assert "Write the Q3 email report; buy benchmarks from DataCo." in page
    assert "Active" in page and "email benchmarks" in page
    for gone in ["Needs your confirmation", "/account/tasks/confirm", "Allow once", "Held for you", "Pay seller",
                 "Refund me", "waiting for you"]:
        assert gone not in page, gone


@pytest.mark.anyio
async def test_old_held_payments_read_as_stopped_and_ask_nothing(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    held_for(conn, user, task_for(conn, user))
    overview = (await client.get("/account")).text
    assert "held for you" not in overview and "waiting for you" not in overview
    payments_page = (await client.get("/account/payments")).text
    assert "HELD" not in payments_page and "BLOCKED" in payments_page
    assert "Judged against the task" in payments_page


@pytest.mark.anyio
async def test_revoking_a_task_stops_it(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    task_id = task_for(conn, user_of(conn, client))
    response = await client.post("/account/tasks/revoke", data={"csrf": await csrf(client), "task_id": task_id})
    assert response.status_code == 303 and task_status(conn, task_id)[0] == "revoked"
    assert "Revoked" in (await client.get("/account/tasks")).text


@pytest.mark.anyio
async def test_nobody_can_revoke_another_users_task(browser, mailer, conn):
    alice = await signed_in(browser, mailer)
    task_id = task_for(conn, user_of(conn, alice))
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    data = {"csrf": await csrf(bob), "task_id": task_id}
    assert (await bob.post("/account/tasks/revoke", data=data)).status_code == 400
    assert task_status(conn, task_id)[0] != "revoked"


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["foreign origin", "no csrf", "wrong csrf"])
async def test_task_forms_refuse_a_post_that_did_not_come_from_our_page(browser, mailer, conn, case):
    client = await signed_in(browser, mailer)
    task_id = task_for(conn, user_of(conn, client))
    token = await csrf(client)
    data = {"task_id": task_id, "csrf": token}
    if case == "foreign origin":
        response = await client.post("/account/tasks/revoke", data=data, headers={"origin": EVIL})
    elif case == "no csrf":
        response = await client.post("/account/tasks/revoke", data={"task_id": task_id})
    else:
        response = await client.post("/account/tasks/revoke", data={**data, "csrf": token[::-1]})
    assert response.status_code == 403 and task_status(conn, task_id)[0] != "revoked"


@pytest.mark.anyio
async def test_a_hostile_task_text_is_shown_as_text(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    task_for(conn, user_of(conn, client), text="<script>alert(1)</script> buy everything")
    page = await client.get("/account/tasks")
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


# Approving a seller: the owner's list is configuration, saved at once; the Rules page shows the wallet model's score


@pytest.mark.anyio
async def test_approving_a_wallet_with_no_history_is_saved_at_once(browser, mailer, conn, fake_chain):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    fake_chain.unused.add(SELLER.lower())
    response = await add_seller(client, "FreshCo", SELLER)
    assert response.status_code == 303 and [s["pay_to"] for s in own_sellers(conn, user)] == [SELLER]
    page = (await client.get("/account/rules")).text
    assert "Approve anyway" not in page and re.search(r"Wallet risk model [01]\.\d\d · escrow only at [01]\.\d\d", page)


@pytest.mark.anyio
async def test_a_wallet_with_history_is_approved_at_once_and_blocking_never_checks(browser, mailer, conn, fake_chain):
    client = await signed_in(browser, mailer)
    assert (await add_seller(client, "NewCo", SELLER)).status_code == 303
    token = await csrf(client, "/account/rules")  # the Rules page itself shows each seller's wallet history
    fake_chain.asked.clear()
    assert (await add_seller(client, "NewCo", SELLER, status="blocked", token=token)).status_code == 303
    assert fake_chain.asked == []


# Payments grouped by task


@pytest.mark.anyio
async def test_payments_are_grouped_by_task_with_counts_and_loose_ones_last(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    first, second = task_for(conn, user, "First task"), task_for(conn, user, "Second task")
    held_for(conn, user, first)
    held_for(conn, user, second, url="http://seller.test/other")
    attempt(conn, user["agent"], url="http://x/loose")
    page = (await client.get("/account/payments")).text
    headings = re.findall(r'<h2 id="task-\d+-h" class="font-semibold">([^<]+)</h2>', page)
    assert headings == [f"Task {second}: Write the Q3 email report.", f"Task {first}: Write the Q3 email report.",
                        "Not under a task"]
    assert page.count("0 paid · 1 stopped") == 2 and "Active" in page and "Needs your confirmation" not in page


@pytest.mark.anyio
async def test_the_rules_page_shows_each_sellers_wallet_history(browser, mailer, conn, fake_chain):
    client = await signed_in(browser, mailer)
    fake_chain.new.add(SELLER.lower())
    await client.post("/account/rules/sellers", data={
        "csrf": await csrf(client, "/account/rules"), "name": "NewCo", "pay_to": SELLER, "status": "approved",
        "confirm": "1",
    })
    page = (await client.get("/account/rules")).text
    assert "Wallet history" in page and "120 token transfers, 5 transactions sent, first seen 2 hours ago" in page
    assert re.search(r"Wallet risk model [01]\.\d\d · escrow only at [01]\.\d\d", page)


# Best value buying: the auto buy switch and the price comparisons on the Tasks page


def auto_buy(conn, user) -> bool:
    row = conn.execute("SELECT auto_buy_best_value FROM agents WHERE id = %s", (user["agent_id"],)).fetchone()
    return bool(row["auto_buy_best_value"])


@pytest.mark.anyio
async def test_auto_buy_is_off_until_the_owner_turns_it_on_and_off_again(browser, mailer, conn):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    page = await client.get("/account/rules")
    assert "Auto buy is <span class=\"font-semibold\">off</span>" in page.text and not auto_buy(conn, user)
    done = await client.post("/account/rules/auto-buy", data={"csrf": form_value(page.text, "csrf"), "auto_buy": "on"})
    assert done.status_code == 303 and done.headers["location"] == "/account/rules?done=auto_buy"
    assert auto_buy(conn, user)
    page = await client.get(done.headers["location"])
    assert "Turn auto buy off" in page.text and "The new setting applies" in page.text
    await client.post("/account/rules/auto-buy", data={"csrf": form_value(page.text, "csrf"), "auto_buy": "off"})
    assert not auto_buy(conn, user)


@pytest.mark.anyio
async def test_auto_buy_refuses_anything_but_on_or_off_and_only_changes_the_owners_own_agent(browser, mailer, conn):
    alice = await signed_in(browser, mailer)
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    response = await alice.post("/account/rules/auto-buy", data={"csrf": await csrf(alice), "auto_buy": "yes"})
    assert response.status_code == 403 and not auto_buy(conn, user_of(conn, alice))
    await alice.post("/account/rules/auto-buy", data={"csrf": await csrf(alice), "auto_buy": "on",
                                                      "agent": user_of(conn, bob)["agent"]})
    assert auto_buy(conn, user_of(conn, alice)) and not auto_buy(conn, user_of(conn, bob))


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{}, {"x-sentinel-section": "tasks_list"}])
async def test_the_tasks_page_shows_each_price_comparison_with_the_pick_and_the_offers(browser, mailer, conn, headers):
    client = await signed_in(browser, mailer)
    user = user_of(conn, client)
    task_id = task_for(conn, user)
    offers = [
        {"name": "Hotels Pro", "source": "catalog", "verdict": "OK", "cost": 150_000, "quality": 90,
         "fit_reason": "Fifty hotels with prices.", "why_not": None, "recommended": True},
        {"name": "<b>Hotels Cheap</b>", "source": "catalog", "verdict": "BLOCKED", "cost": 20_000, "quality": None,
         "fit_reason": None, "why_not": "Sentinel would not pay it for this task.", "recommended": False},
    ]
    mandates.save_comparison(conn, task_id, "Tokyo hotel prices", 1, 50, offers, ["http://m.test/pro"],
                             ("http://m.test/pro", "Hotels Pro", 150_000),
                             ("http://m.test/lite", "Hotels Lite", 50_000),
                             "Best value: Hotels Pro at $0.15 for this job, scoring 90 of 100.")
    page = await client.get("/account/tasks", headers=headers)
    assert page.status_code == 200
    assert "Price comparison: Tokyo hotel prices" in page.text
    assert "picked Hotels Pro ($0.15) over Hotels Lite ($0.05)" in page.text
    assert "Fifty hotels with prices." in page.text and "· blocked" in page.text
    assert "&lt;b&gt;Hotels Cheap&lt;/b&gt;" in page.text and "<b>Hotels Cheap</b>" not in page.text
    bob = await signed_in(browser, mailer, "bob@example.com", BOB_IP)
    assert "Tokyo hotel prices" not in (await bob.get("/account/tasks", headers=headers)).text
