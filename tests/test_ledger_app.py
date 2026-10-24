"""Tests for the owner's ledger page over HTTP: the password gate, the forms, escaping and the CSV."""
from contextlib import contextmanager

import pytest
from conftest import ASSET, DATACO, NETWORK
from fastapi.testclient import TestClient

from sentinel import auth, ledger_app, payments
from sentinel.guard import Quote

PASSWORD = "correct horse battery staple"
NEW_SELLER = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def client(conn):
    @contextmanager
    def same_connection():
        yield conn

    return TestClient(ledger_app.create_app(connect=same_connection, password=PASSWORD), follow_redirects=False)


@pytest.fixture
def owner_client(client):
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    return client


def pay(conn, reason: str = "need benchmarks for the report", pay_to: str = DATACO) -> int:
    return payments.authorize(conn, "research-agent", reason, Quote("http://x/a", pay_to, 500_000, ASSET, NETWORK, 300))


def test_a_short_password_refuses_to_start():
    with pytest.raises(RuntimeError):
        ledger_app.create_app(password="short")


@pytest.mark.parametrize("path", ["/", "/payments.csv"])
def test_without_signing_in_every_page_sends_you_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_forms_cannot_be_posted_without_signing_in(client, conn):
    r = client.post("/sellers", data={"name": "Evil", "pay_to": NEW_SELLER, "status": "approved"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert conn.execute("SELECT 1 FROM sellers WHERE pay_to = %s", (NEW_SELLER,)).fetchone() is None


def test_a_wrong_password_or_the_agent_token_is_refused(client, conn):
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    agent_token = auth.issue_token(conn, "research-agent")
    assert client.post("/login", data={"password": agent_token}).status_code == 401
    assert client.get("/").status_code == 303


def test_a_forged_cookie_is_refused(client):
    client.cookies.set(ledger_app.COOKIE, "0" * 64)
    assert client.get("/").status_code == 303


def test_signed_in_page_shows_payments_with_untrusted_text_escaped(owner_client, conn):
    pay(conn, reason="<script>alert('owned')</script> need data")
    r = owner_client.get("/")
    assert r.status_code == 200 and "DataCo" in r.text and "$0.50" in r.text
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_approving_a_seller_from_the_page_lists_it(owner_client, conn):
    r = owner_client.post("/sellers", data={"name": "NewCo", "pay_to": NEW_SELLER, "status": "approved"})
    assert r.status_code == 303
    row = conn.execute("SELECT name, status, added_by FROM sellers WHERE pay_to = %s", (NEW_SELLER,)).fetchone()
    assert (row["name"], row["status"], row["added_by"]) == ("NewCo", "approved", "owner")
    assert pay(conn, pay_to=NEW_SELLER)


def test_a_bad_address_shows_an_error_and_changes_nothing(owner_client, conn):
    r = owner_client.post("/sellers", data={"name": "NewCo", "pay_to": "0x123", "status": "approved"})
    assert r.status_code == 400 and "not a wallet address" in r.text
    assert conn.execute("SELECT 1 FROM sellers WHERE name = 'NewCo'").fetchone() is None


def test_limits_form_saves_dollar_amounts_as_atomic_units(owner_client, conn):
    r = owner_client.post("/agents/research-agent/limits", data={"cap": "$2.50", "budget": "20"})
    assert r.status_code == 303
    row = conn.execute("SELECT per_payment_cap, weekly_budget FROM agents WHERE name = 'research-agent'").fetchone()
    assert (str(row["per_payment_cap"]), str(row["weekly_budget"])) == ("2.500000", "20.000000")


def test_limits_form_rejects_a_cap_above_the_budget(owner_client):
    r = owner_client.post("/agents/research-agent/limits", data={"cap": "60", "budget": "50"})
    assert r.status_code == 400 and "cannot be more than the weekly budget" in r.text


def test_payments_csv_downloads_one_row_per_attempt(owner_client, conn):
    pay(conn)
    r = owner_client.get("/payments.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,created_at,agent,reason") and len(lines) == 2


def test_sign_out_clears_the_session(owner_client):
    assert owner_client.post("/logout").status_code == 303
    assert owner_client.get("/").status_code == 303
