"""Tests for sign in: settings, email rules, magic links, sessions, CSRF, rate limits and mail, then the same features
over HTTP through the whole app.

The first part runs below the web app: pure functions directly, and the link and rate limit SQL on the rolled back
test connection, aging rows with UPDATE instead of sleeping (every window is decided by SQL now()). The HTTP part
drives web/app.py with conftest's browser() clients (one cookie jar and one client IP each). Nothing here sends mail
or touches the network: Resend is an httpx.MockTransport and the app gets a recording mailer. main() is tested in
test_mcp_server.py.
"""
import io
import json
import logging
import logging.config
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn.config
from conftest import BROWSER_IP, TEST_BASE_URL, access_token_for, form_value, sign_in
from fastmcp.utilities.asgi_transport import run_asgi_lifespan

from sentinel import db
from sentinel.web import ConfigError, Settings, auth, dev_mode, is_loopback, load_settings, missing_env, users
from sentinel.web import app as web_app
from sentinel.web import mailer as mailing
from sentinel.web import ratelimit as rl

SESSION_SECRET = "s" * 40
TOKEN_SECRET = "t" * 40
UUID = "2f1c3d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


def dev_env(**extra: str) -> dict[str, str]:
    """A complete dev mode environment with test secrets (never real ones)."""
    env = {
        "DATABASE_URL": "postgresql://test",
        "PUBLIC_BASE_URL": TEST_BASE_URL,
        "SESSION_SECRET": SESSION_SECRET,
        "TOKEN_HASH_SECRET": TOKEN_SECRET,
    }
    env.update(extra)
    return env


def prod_env(**extra: str) -> dict[str, str]:
    """A complete production environment (https, mail configured) with test values."""
    env = dev_env(
        PUBLIC_BASE_URL="https://sentinel.example.com",
        RESEND_API_KEY="re_test_key",
        MAIL_FROM="Sentinel <signin@example.com>",
    )
    env.update(extra)
    return env


@pytest.fixture
def https_settings() -> Settings:
    """Production settings: https base URL, public bind host, Resend configured."""
    return load_settings(prod_env(), "0.0.0.0")


def problems(env: dict[str, str], bind_host: str = "127.0.0.1") -> list[str]:
    """The problems load_settings reports for an environment (fails the test if it loads)."""
    with pytest.raises(ConfigError) as info:
        load_settings(env, bind_host)
    return info.value.problems


# Settings


def test_dev_settings_load(settings):
    assert settings.base_url == TEST_BASE_URL
    assert settings.resource_url == TEST_BASE_URL + "/mcp"
    assert settings.dev_mode and not settings.https
    assert settings.resend_api_key is None and settings.mail_from is None
    assert settings.session_secret == SESSION_SECRET.encode()
    assert settings.warnings == ()


def test_production_settings_load(https_settings):
    assert https_settings.base_url == "https://sentinel.example.com"
    assert https_settings.https and not https_settings.dev_mode
    assert https_settings.resend_api_key == "re_test_key"


def test_dev_mode_needs_both_hosts_loopback():
    assert dev_mode("http://localhost:8000", "127.0.0.1")
    assert dev_mode("http://127.0.0.1:8000", "localhost")
    assert dev_mode("http://[::1]:8000", "::1")
    assert not dev_mode("http://localhost:8000", "0.0.0.0")
    assert not dev_mode("https://sentinel.example.com", "127.0.0.1")
    assert not dev_mode(None, "127.0.0.1")
    assert is_loopback("LOCALHOST") and is_loopback("[::1]")
    assert not is_loopback(None) and not is_loopback("") and not is_loopback("10.0.0.1")


def test_loopback_url_with_public_bind_host_is_not_dev_mode():
    found = problems(dev_env(), "0.0.0.0")
    assert any("RESEND_API_KEY" in p for p in found) and any("MAIL_FROM" in p for p in found)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Sentinel.Example.COM", "https://sentinel.example.com"),
        ("https://sentinel.example.com/", "https://sentinel.example.com"),
        ("HTTPS://sentinel.example.com:8443/", "https://sentinel.example.com:8443"),
        ("https://sentinel.example.com:443", "https://sentinel.example.com"),
        ("  https://sentinel.example.com  ", "https://sentinel.example.com"),
    ],
)
def test_base_url_is_normalized(raw, expected):
    assert load_settings(prod_env(PUBLIC_BASE_URL=raw), "0.0.0.0").base_url == expected


def test_loopback_base_url_normalized():
    assert load_settings(dev_env(PUBLIC_BASE_URL="http://LocalHost:8000/"), "127.0.0.1").base_url == TEST_BASE_URL
    assert load_settings(dev_env(PUBLIC_BASE_URL="http://[::1]:8000"), "::1").base_url == "http://[::1]:8000"


@pytest.mark.parametrize(
    ("raw", "word"),
    [
        ("http://sentinel.example.com", "https"),
        ("ftp://sentinel.example.com", "https"),
        ("https://sentinel.example.com/app", "path"),
        ("https://sentinel.example.com/?x=1", "query"),
        ("https://sentinel.example.com/?", "query"),
        ("https://sentinel.example.com/#top", "fragment"),
        ("https://user:pw@sentinel.example.com", "password"),
        ("https://user@sentinel.example.com", "user name"),
        ("https://", "host"),
        ("https://sentinel.example.com:99999", "valid"),
    ],
)
def test_bad_base_urls_are_refused(raw, word):
    found = problems(prod_env(PUBLIC_BASE_URL=raw), "0.0.0.0")
    assert any(word in p for p in found), found


def test_secrets_must_be_long_and_different():
    assert any("SESSION_SECRET" in p and "32" in p for p in problems(dev_env(SESSION_SECRET="short")))
    assert any("TOKEN_HASH_SECRET" in p and "32" in p for p in problems(dev_env(TOKEN_HASH_SECRET="x" * 31)))
    assert any("different" in p for p in problems(dev_env(TOKEN_HASH_SECRET=SESSION_SECRET)))
    load_settings(dev_env(SESSION_SECRET="a" * 32), "127.0.0.1")  # exactly 32 is enough


def test_mail_settings_required_outside_dev_mode():
    env = prod_env()
    del env["RESEND_API_KEY"], env["MAIL_FROM"]
    found = problems(env, "0.0.0.0")
    assert any("RESEND_API_KEY" in p for p in found) and any("MAIL_FROM" in p for p in found)


def test_mail_from_required_whenever_a_key_is_set():
    found = problems(dev_env(RESEND_API_KEY="re_test_key"))
    assert any("MAIL_FROM" in p for p in found)
    loaded = load_settings(dev_env(RESEND_API_KEY="re_test_key", MAIL_FROM="a@example.com"), "127.0.0.1")
    assert loaded.resend_api_key == "re_test_key" and loaded.dev_mode


def test_allow_local_urls_refused_outside_dev_mode():
    assert any("SENTINEL_ALLOW_LOCAL_URLS" in p for p in problems(prod_env(SENTINEL_ALLOW_LOCAL_URLS="1"), "0.0.0.0"))
    assert load_settings(dev_env(SENTINEL_ALLOW_LOCAL_URLS="1"), "127.0.0.1").dev_mode


def test_forwarded_allow_ips_star_warns():
    loaded = load_settings(prod_env(FORWARDED_ALLOW_IPS="*"), "0.0.0.0")
    assert len(loaded.warnings) == 1 and "FORWARDED_ALLOW_IPS" in loaded.warnings[0]
    assert load_settings(prod_env(FORWARDED_ALLOW_IPS="10.0.0.5"), "0.0.0.0").warnings == ()


def test_forwarded_allow_ips_unset_warns_outside_dev_mode():
    for env in [prod_env(), prod_env(FORWARDED_ALLOW_IPS="  ")]:
        loaded = load_settings(env, "0.0.0.0")
        assert len(loaded.warnings) == 1 and "FORWARDED_ALLOW_IPS is not set" in loaded.warnings[0]
        assert "site wide" in loaded.warnings[0] and "Anthropic" in loaded.warnings[0]
    assert load_settings(dev_env(), "127.0.0.1").warnings == ()  # a local run has no proxy


def test_every_problem_is_reported_at_once():
    env = {
        "PUBLIC_BASE_URL": "http://sentinel.example.com/path?q=1",
        "SESSION_SECRET": "short",
        "SENTINEL_ALLOW_LOCAL_URLS": "1",
    }
    found = problems(env, "0.0.0.0")
    text = " | ".join(found)
    for word in ("path", "query", "https", "SESSION_SECRET", "TOKEN_HASH_SECRET", "RESEND_API_KEY", "MAIL_FROM",
                 "SENTINEL_ALLOW_LOCAL_URLS"):
        assert word in text, word
    with pytest.raises(ConfigError) as info:
        load_settings(env, "0.0.0.0")
    assert str(info.value) == "; ".join(found)


def test_missing_env_names_every_missing_variable():
    assert missing_env({}, "0.0.0.0") == [
        "DATABASE_URL", "PUBLIC_BASE_URL", "SESSION_SECRET", "TOKEN_HASH_SECRET", "RESEND_API_KEY", "MAIL_FROM",
    ]
    # With PUBLIC_BASE_URL missing, dev mode is judged from the bind host alone.
    assert missing_env({}, "127.0.0.1") == ["DATABASE_URL", "PUBLIC_BASE_URL", "SESSION_SECRET", "TOKEN_HASH_SECRET"]
    assert missing_env(dev_env(), "127.0.0.1") == []
    assert missing_env(dev_env(), "0.0.0.0") == ["RESEND_API_KEY", "MAIL_FROM"]
    assert missing_env(dev_env(RESEND_API_KEY="re_test_key"), "127.0.0.1") == ["MAIL_FROM"]
    assert missing_env(prod_env(SESSION_SECRET="  "), "0.0.0.0") == ["SESSION_SECRET"]
    assert missing_env(prod_env(), "0.0.0.0") == []


def test_keys_differ_per_purpose_and_per_secret(settings):
    from sentinel.web import SESSION_PURPOSES, TOKEN_PURPOSES

    keys = {purpose: settings.key(purpose) for purpose in SESSION_PURPOSES | TOKEN_PURPOSES}
    assert len(set(keys.values())) == len(keys)
    assert all(len(k) == 32 for k in keys.values())
    other = load_settings(dev_env(SESSION_SECRET="u" * 40, TOKEN_HASH_SECRET="v" * 40), "127.0.0.1")
    assert all(other.key(purpose) != key for purpose, key in keys.items())
    # A session purpose depends only on SESSION_SECRET, a token purpose only on TOKEN_HASH_SECRET.
    swapped_token = load_settings(dev_env(TOKEN_HASH_SECRET="v" * 40), "127.0.0.1")
    assert swapped_token.key("session") == settings.key("session")
    assert swapped_token.key("magic-link") != settings.key("magic-link")
    with pytest.raises(ValueError):
        settings.key("nonsense")


# Email addresses, limit keys and next


def test_normalize_email():
    assert auth.normalize_email("  Alice.Smith@Example.COM ") == ("Alice.Smith@example.com", "alice.smith@example.com")
    email, key = auth.normalize_email("bob@Bücher.de")
    assert email == "bob@xn--bcher-kva.de" and key == email


@pytest.mark.parametrize("raw", ["", "   ", "not-an-email", "a@b", "a@@example.com", "ünï@example.com",
                                 "a" * 250 + "@example.com", "x@" + "a" * 250 + ".com"])
def test_invalid_emails_are_refused(raw):
    with pytest.raises(ValueError) as info:
        auth.normalize_email(raw)
    assert str(info.value)


def test_limit_key_folds_tags_and_gmail_dots():
    assert auth.limit_key("alice+news@example.com") == "alice@example.com"
    assert auth.limit_key("alice+a+b@example.com") == "alice@example.com"
    assert auth.limit_key("a.lice@example.com") == "a.lice@example.com"  # dots only matter on Gmail
    assert auth.limit_key("a.l.ice+x@gmail.com") == "alice@gmail.com"
    assert auth.limit_key("alice@googlemail.com") == "alice@gmail.com"
    assert auth.limit_key("Alice@Example.com") == "alice@example.com"


@pytest.mark.parametrize("raw", ["/account", "/account/payments", "/account/rules", "/account/books",
                                 "/oauth/consent?req=abc.DEF_12-3", "/oauth/consent?req=" + "a" * 4096])
def test_safe_next_accepts_the_allow_list(raw):
    assert auth.safe_next(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [None, "", "//evil.com", "/\\evil.com", "https://evil.com", "http://localhost:8000/account", "/account/",
     "/accounts", "/account?x=1", "/account/payments.csv", "/account/rules/limits", "/account/books/",
     "/account/rules?done=limits", "/Account/rules", "/oauth/consent", "/oauth/consent?req=", "/oauth/consent?req=a b",
     "/oauth/consent?req=a&x=1", "/oauth/consent?req=a\n", "/signin", "/oauth/consent?req=" + "a" * 4097],
)
def test_safe_next_refuses_everything_else(raw):
    assert auth.safe_next(raw) is None


def test_digest_and_b64():
    assert auth.digest(b"k", "v") != auth.digest(b"k2", "v")
    assert len(auth.digest(b"k", "v")) == 64
    assert auth.b64(b"\xff\xfe") == "__4" and "=" not in auth.b64(b"x")


# Magic links


def new_link(conn, settings, email="Alice@Example.com", next_path=None, ip="203.0.113.10"):
    """Issue a link the way /signin does and return (token, id)."""
    address, key = auth.normalize_email(email)
    return auth.issue_link(conn, settings, email=address, email_key=key, limit_key=auth.limit_key(key),
                           ip_hash=rl.ip_hash(settings, ip), next_path=next_path)


def age_links(conn, minutes: int) -> None:
    """Move every link back in time (created and expiry together, so the table's check still holds)."""
    conn.execute(
        "UPDATE magic_links SET created_at = created_at - make_interval(mins => %s),"
        " expires_at = expires_at - make_interval(mins => %s)",
        (minutes, minutes),
    )


def test_link_is_stored_only_as_a_hash(conn, settings):
    token, link_id = new_link(conn, settings, next_path="/account")
    assert auth.TOKEN_RE.match(token)
    row = conn.execute("SELECT * FROM magic_links WHERE id = %s", (link_id,)).fetchone()
    assert row["token_hash"] == auth.digest(settings.key("magic-link"), token)
    assert all(token not in str(value) for value in row.values())
    assert row["email"] == "Alice@example.com" and row["email_key"] == "alice@example.com"
    assert row["ip_hash"] != "203.0.113.10" and "203.0.113" not in row["ip_hash"]
    lifetime = conn.execute("SELECT expires_at - created_at AS d FROM magic_links WHERE id = %s", (link_id,))
    assert lifetime.fetchone()["d"].total_seconds() == 15 * 60


def test_peek_consumes_nothing(conn, settings):
    token, link_id = new_link(conn, settings, next_path="/account")
    for _ in range(3):
        assert auth.peek_link(conn, settings, token) == {
            "id": link_id, "email": "Alice@example.com", "email_key": "alice@example.com", "next_path": "/account",
        }
    assert conn.execute("SELECT used_at FROM magic_links WHERE id = %s", (link_id,)).fetchone()["used_at"] is None


def test_consume_is_single_use(conn, settings):
    token, link_id = new_link(conn, settings)
    first = auth.consume_link(conn, settings, token)
    assert first is not None and first["id"] == link_id and first["next_path"] is None
    assert auth.consume_link(conn, settings, token) is None
    assert auth.peek_link(conn, settings, token) is None


def test_links_expire_after_15_minutes(conn, settings):
    token, _ = new_link(conn, settings)
    age_links(conn, 14)
    assert auth.peek_link(conn, settings, token) is not None
    age_links(conn, 2)
    assert auth.peek_link(conn, settings, token) is None
    assert auth.consume_link(conn, settings, token) is None


def test_tampered_or_malformed_tokens_are_refused(conn, settings):
    token, _ = new_link(conn, settings)
    flipped = ("A" if token[0] != "A" else "B") + token[1:]
    upper = token.upper() if token.upper() != token else "x"
    for bad in [flipped, token[:-1], token + "x", "", "!" * 43, token + "\n", upper]:
        assert auth.peek_link(conn, settings, bad) is None
        assert auth.consume_link(conn, settings, bad) is None
    other = load_settings(dev_env(TOKEN_HASH_SECRET="v" * 40), "127.0.0.1")
    assert auth.peek_link(conn, other, token) is None  # a different hash key finds nothing
    assert auth.peek_link(conn, settings, token) is not None


def test_next_path_constraint_refuses_protocol_relative(conn, settings):
    with pytest.raises(db.IntegrityError), db.transaction(conn):
        new_link(conn, settings, next_path="//evil.com")


# Sessions, CSRF and origin


def test_session_round_trip(settings):
    value = auth.make_session(settings, UUID, 3)
    session = auth.read_session(settings, value)
    assert session is not None
    assert (session.user_id, session.epoch, session.value) == (UUID, 3, value)
    assert value.startswith(f"v2.{UUID}.3.")


def test_two_sessions_for_the_same_user_in_the_same_second_differ(settings, monkeypatch):
    monkeypatch.setattr(auth, "clock", lambda: 1_000_000.0)
    first, second = auth.make_session(settings, UUID, 0), auth.make_session(settings, UUID, 0)
    assert first != second
    assert auth.read_session(settings, first) is not None and auth.read_session(settings, second) is not None
    token = auth.csrf_token(settings, first, "account")
    assert auth.check_csrf(settings, first, "account", token)
    assert not auth.check_csrf(settings, second, "account", token)


def test_old_v1_sessions_without_a_nonce_are_refused(settings):
    body = f"v1.{UUID}.0.{int(auth.clock()) + 3600}"
    assert auth.read_session(settings, f"{body}.{auth._sign(settings.key('session'), body)}") is None


def test_session_expires_after_24_hours(settings, monkeypatch):
    monkeypatch.setattr(auth, "clock", lambda: 1_000_000.0)
    value = auth.make_session(settings, UUID, 0)
    assert auth.read_session(settings, value).expires == 1_000_000 + 24 * 3600
    monkeypatch.setattr(auth, "clock", lambda: 1_000_000.0 + 24 * 3600 - 1)
    assert auth.read_session(settings, value) is not None
    monkeypatch.setattr(auth, "clock", lambda: 1_000_000.0 + 24 * 3600)
    assert auth.read_session(settings, value) is None


def test_tampered_sessions_are_refused(settings):
    value = auth.make_session(settings, UUID, 0)
    version, user_id, epoch, expires, nonce, signature = value.split(".")
    other_user = "00000000-0000-4000-8000-000000000000"
    middle = len(signature) // 2  # a middle character, so the edit always changes the signed bytes
    for bad in [
        f"{version}.{other_user}.{epoch}.{expires}.{nonce}.{signature}",
        f"{version}.{user_id}.1.{expires}.{nonce}.{signature}",
        f"{version}.{user_id}.{epoch}.{int(expires) + 999}.{nonce}.{signature}",
        f"{version}.{user_id}.{epoch}.{expires}.{'A' * 16 if nonce != 'A' * 16 else 'B' * 16}.{signature}",
        f"{version}.{user_id}.{epoch}.{expires}.{nonce}."
        + signature[:middle] + ("A" if signature[middle] != "A" else "B") + signature[middle + 1:],
        f"{version}.{user_id}.{epoch}.{expires}.{nonce}.",
    ]:
        assert auth.read_session(settings, bad) is None
    other = load_settings(dev_env(SESSION_SECRET="u" * 40), "127.0.0.1")
    assert auth.read_session(other, value) is None


def test_badly_formed_sessions_are_refused(settings):
    good = auth.make_session(settings, UUID, 0)
    for bad in [None, "", "garbage", "v1" + good[2:], good + ".extra", "v2.a.b.c.d", "x" * 600, good.replace(".", "-")]:
        assert auth.read_session(settings, bad) is None


def test_session_with_non_numeric_fields_is_refused_even_if_signed(settings):
    for body in [f"v2.{UUID}.-1.99999999999.{'n' * 16}", f"v2.{UUID}.0.99999999999.short"]:
        signed = f"{body}.{auth._sign(settings.key('session'), body)}"
        assert auth.read_session(settings, signed) is None


def test_cookie_name_and_flags(settings, https_settings):
    assert auth.session_cookie_name(settings) == "sentinel_session"
    assert auth.session_cookie_name(https_settings) == "__Host-sentinel_session"
    assert auth.cookie_kwargs(settings, 60) == {
        "httponly": True, "samesite": "lax", "path": "/", "secure": False, "max_age": 60,
    }
    assert auth.cookie_kwargs(https_settings, 86400)["secure"] is True


def test_csrf_is_bound_to_session_and_purpose(settings):
    session_a = auth.make_session(settings, UUID, 0)
    session_b = auth.make_session(settings, UUID, 1)
    token = auth.csrf_token(settings, session_a, "account")
    assert auth.check_csrf(settings, session_a, "account", token)
    assert not auth.check_csrf(settings, session_b, "account", token)
    assert not auth.check_csrf(settings, session_a, "consent:nonce", token)
    assert not auth.check_csrf(settings, session_a, "account", None)
    assert not auth.check_csrf(settings, session_a, "account", "")
    assert not auth.check_csrf(settings, session_a, "account", token[:-1])
    assert not auth.check_csrf(settings, session_a, "account", "ü" * 43)
    assert auth.csrf_token(settings, session_a, "consent:n1") != auth.csrf_token(settings, session_a, "consent:n2")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Origin": TEST_BASE_URL}, True),
        ({"origin": TEST_BASE_URL, "sec-fetch-site": "cross-site"}, True),
        ({"Origin": "http://evil.example"}, False),
        ({"Origin": TEST_BASE_URL + "/"}, False),
        ({"Origin": "http://localhost:8001"}, False),
        ({"Origin": "null"}, False),
        ({"Origin": "null", "Sec-Fetch-Site": "same-origin"}, False),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({}, False),
    ],
)
def test_same_origin(settings, headers, expected):
    assert auth.same_origin(settings, headers) is expected


def test_same_origin_with_starlette_headers(settings):
    from starlette.datastructures import Headers

    assert auth.same_origin(settings, Headers({"origin": TEST_BASE_URL}))
    assert not auth.same_origin(settings, Headers({"origin": "https://evil.example"}))


# Rate limits


@pytest.mark.parametrize(
    ("ip", "key"),
    [
        ("203.0.113.10", "203.0.113.10"),
        ("::ffff:203.0.113.10", "203.0.113.10"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("fe80::1%eth0", "fe80::/64"),
        ("not an ip", "unknown"),
        ("", "unknown"),
    ],
)
def test_network_key(ip, key):
    assert rl.network_key(ip) == key


def test_network_key_groups_ipv6_by_48_for_registrations():
    assert rl.network_key("2001:db8:1:2:3:4:5:6", 48) == "2001:db8:1::/48"
    assert rl.network_key("::ffff:203.0.113.10", 48) == "203.0.113.10"
    assert rl.network_key("garbage", 48) == "unknown"


def test_registration_ip_hash_groups_by_48(settings):
    assert rl.registration_ip_hash(settings, "2001:db8:1:2::1") == rl.registration_ip_hash(settings, "2001:db8:1:ff::9")
    assert rl.registration_ip_hash(settings, "2001:db8:1::1") != rl.registration_ip_hash(settings, "2001:db8:2::1")
    assert rl.registration_ip_hash(settings, "203.0.113.10") == rl.ip_hash(settings, "203.0.113.10")
    assert rl.ip_hash(settings, "2001:db8:1:2::1") != rl.ip_hash(settings, "2001:db8:1:ff::9")  # links stay /64


def test_ip_hash_groups_like_network_key(settings):
    assert rl.ip_hash(settings, "203.0.113.10") == rl.ip_hash(settings, "::ffff:203.0.113.10")
    assert rl.ip_hash(settings, "2001:db8:1:2::1") == rl.ip_hash(settings, "2001:db8:1:2:ffff::9")
    assert rl.ip_hash(settings, "2001:db8:1:2::1") != rl.ip_hash(settings, "2001:db8:1:3::1")
    assert rl.ip_hash(settings, "203.0.113.10") != rl.ip_hash(settings, "203.0.113.11")
    assert "203.0.113.10" not in rl.ip_hash(settings, "203.0.113.10")


@pytest.mark.parametrize(
    ("ip", "exempt"),
    [
        ("160.79.104.0", True),
        ("160.79.111.255", True),
        ("::ffff:160.79.105.7", True),
        ("160.79.112.0", False),
        ("160.79.103.255", False),
        ("2001:db8::1", False),
        ("garbage", False),
    ],
)
def test_is_exempt(ip, exempt):
    assert rl.is_exempt(ip) is exempt


def fill_links(conn, count: int, *, limit_key: str = "", ip_hash: str = "") -> None:
    """Insert counted magic_links rows directly (distinct keys and IPs unless given), to reach limits cheaply."""
    conn.execute(
        "INSERT INTO magic_links (token_hash, email, email_key, limit_key, ip_hash, expires_at)"
        " SELECT md5(random()::text || g), 'f@example.com', 'f@example.com',"
        "        coalesce(nullif(%s, ''), 'filler' || g || '@example.com'), coalesce(nullif(%s, ''), 'ip' || g),"
        "        now() + interval '15 minutes'"
        " FROM generate_series(1, %s) g",
        (limit_key, ip_hash, count),
    )


def request_link(conn, settings, email: str, ip: str = "203.0.113.10") -> None:
    """Check the limits and issue a link in one transaction, the way /signin does."""
    address, key = auth.normalize_email(email)
    with db.transaction(conn):
        lk, ih = auth.limit_key(key), rl.ip_hash(settings, ip)
        rl.check_link_limits(conn, limit_key=lk, ip_hash=ih)
        auth.issue_link(conn, settings, email=address, email_key=key, limit_key=lk, ip_hash=ih, next_path=None)


def link_count(conn) -> int:
    """How many magic links exist in this test's transaction."""
    return int(conn.execute("SELECT count(*) AS n FROM magic_links").fetchone()["n"])


def test_three_links_per_address_including_tag_variants(conn, settings):
    for email in ["alice@example.com", "Alice+one@example.com", "alice+two@example.com"]:
        request_link(conn, settings, email, ip=f"198.51.100.{len(email)}")
    before = link_count(conn)
    with pytest.raises(rl.RateLimited) as info:
        request_link(conn, settings, "alice+three@example.com", ip="192.0.2.99")
    assert link_count(conn) == before  # nothing stored
    assert info.value.retry_after == 15 * 60  # all rows share this transaction's now()
    request_link(conn, settings, "bob@example.com")  # another address is unaffected


def test_gmail_dots_count_as_one_address(conn, settings):
    for email in ["a.lice@gmail.com", "alice@googlemail.com", "al.ice+x@gmail.com"]:
        request_link(conn, settings, email)
    with pytest.raises(rl.RateLimited):
        request_link(conn, settings, "alice@gmail.com")


def test_retry_after_is_when_the_oldest_counted_row_leaves(conn, settings):
    for _ in range(3):
        request_link(conn, settings, "carol@example.com")
    conn.execute("UPDATE magic_links SET created_at = created_at - interval '10 minutes'"
                 " WHERE id = (SELECT min(id) FROM magic_links)")
    with pytest.raises(rl.RateLimited) as info:
        request_link(conn, settings, "carol@example.com")
    assert info.value.retry_after == 5 * 60


def test_address_window_follows_sql_now(conn, settings):
    for _ in range(3):
        request_link(conn, settings, "dave@example.com")
    age_links(conn, 16)
    request_link(conn, settings, "dave@example.com")  # the 15 minute window has passed


def test_ten_links_per_address_per_day(conn, settings):
    fill_links(conn, 10, limit_key="erin@example.com")
    age_links(conn, 60)  # out of the 15 minute window, still inside the day
    with pytest.raises(rl.RateLimited) as info:
        request_link(conn, settings, "erin+x@example.com")
    assert info.value.retry_after == 23 * 3600
    age_links(conn, 23 * 60)
    request_link(conn, settings, "erin@example.com")


def test_sixty_links_per_ip(conn, settings):
    fill_links(conn, 59, ip_hash=rl.ip_hash(settings, "198.51.100.7"))
    request_link(conn, settings, "frank@example.com", ip="::ffff:198.51.100.7")  # the 60th, as IPv4 mapped
    with pytest.raises(rl.RateLimited) as info:
        request_link(conn, settings, "grace@example.com", ip="198.51.100.7")
    assert 1 <= info.value.retry_after <= 3600
    request_link(conn, settings, "grace@example.com", ip="198.51.100.8")


def test_ipv6_is_limited_per_64(conn, settings):
    fill_links(conn, 60, ip_hash=rl.ip_hash(settings, "2001:db8:1:2::1"))
    with pytest.raises(rl.RateLimited):
        request_link(conn, settings, "heidi@example.com", ip="2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    request_link(conn, settings, "heidi@example.com", ip="2001:db8:1:3::1")


def test_ip_window_follows_sql_now(conn, settings):
    fill_links(conn, 60, ip_hash=rl.ip_hash(settings, "198.51.100.7"))
    age_links(conn, 61)
    request_link(conn, settings, "ivan@example.com", ip="198.51.100.7")


def test_global_link_cap(conn, settings):
    fill_links(conn, 300)
    with pytest.raises(rl.RateLimited) as info:
        request_link(conn, settings, "judy@example.com", ip="192.0.2.1")
    assert info.value.retry_after == 3600
    age_links(conn, 61)
    request_link(conn, settings, "judy@example.com", ip="192.0.2.1")


def test_rate_limited_never_waits_less_than_a_second():
    assert rl.RateLimited(0).retry_after == 1
    assert rl.RateLimited(-5).retry_after == 1
    assert rl.RateLimited(42).retry_after == 42


def fill_clients(conn, count: int, ip_hash: str | None = None) -> None:
    """Insert registered OAuth clients directly (distinct IPs unless given)."""
    conn.execute(
        "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
        " SELECT gen_random_uuid()::text, '{}'::jsonb, 'none', coalesce(%s, 'ip' || g)"
        " FROM generate_series(1, %s) g",
        (ip_hash, count),
    )


def test_limit_checks_refuse_to_run_outside_a_transaction(test_schema, settings):
    ih = rl.ip_hash(settings, "203.0.113.10")
    with db.connection() as bare:  # autocommit: no transaction, so an advisory lock would not hold
        with pytest.raises(RuntimeError, match="db.transaction"):
            rl.check_link_limits(bare, limit_key="a@example.com", ip_hash=ih)
        with pytest.raises(RuntimeError, match="db.transaction"):
            rl.check_registration_limits(bare, ip_hash=ih, exempt=False)


def test_twenty_registrations_per_ip_unless_exempt(conn, settings):
    ip = rl.ip_hash(settings, "203.0.113.50")
    fill_clients(conn, 19, ip)
    rl.check_registration_limits(conn, ip_hash=ip, exempt=False)
    fill_clients(conn, 1, ip)
    with pytest.raises(rl.RateLimited) as info:
        rl.check_registration_limits(conn, ip_hash=ip, exempt=False)
    assert info.value.retry_after == 3600
    rl.check_registration_limits(conn, ip_hash=ip, exempt=True)
    rl.check_registration_limits(conn, ip_hash=rl.ip_hash(settings, "203.0.113.51"), exempt=False)
    conn.execute("UPDATE oauth_clients SET created_at = created_at - interval '61 minutes'")
    rl.check_registration_limits(conn, ip_hash=ip, exempt=False)


def test_ordinary_registrations_cannot_use_up_the_exempt_budget(conn, settings):
    fill_clients(conn, 500)
    with pytest.raises(rl.RateLimited) as info:
        rl.check_registration_limits(conn, ip_hash=rl.registration_ip_hash(settings, "198.51.100.1"), exempt=False)
    assert info.value.retry_after == 3600
    rl.check_registration_limits(conn, ip_hash=rl.registration_ip_hash(settings, "160.79.104.1"), exempt=True)


def test_exempt_registrations_have_a_separate_overall_cap(conn, settings):
    exempt_hash = rl.registration_ip_hash(settings, "160.79.104.1")
    assert rl.stored_registration_hash(exempt_hash, True) == rl.EXEMPT_MARK + exempt_hash
    assert rl.stored_registration_hash(exempt_hash, False) == exempt_hash
    fill_clients(conn, 499, rl.stored_registration_hash(exempt_hash, True))
    rl.check_registration_limits(conn, ip_hash=exempt_hash, exempt=True)
    fill_clients(conn, 1, rl.stored_registration_hash(exempt_hash, True))
    with pytest.raises(rl.RateLimited):
        rl.check_registration_limits(conn, ip_hash=exempt_hash, exempt=True)
    rl.check_registration_limits(conn, ip_hash=rl.registration_ip_hash(settings, "198.51.100.1"), exempt=False)


# Mail


class Recorder:
    """An httpx.MockTransport handler that answers with a scripted list of statuses or network errors."""

    def __init__(self, *answers: int | type[httpx.TransportError]) -> None:
        """Answers are used in order; the last one repeats."""
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request, then raise or answer with the next scripted status."""
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, type):
            raise answer("boom", request=request)
        return httpx.Response(answer, json={"id": "e1", "message": "secret detail from Resend"})


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Skip the retry backoff and record each delay."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(mailing, "sleep", fake_sleep)
    return delays


def resend(recorder: Recorder, **kw: Any) -> mailing.ResendMailer:
    """A ResendMailer answering from the recorder."""
    return mailing.ResendMailer("re_secret_key", "Sentinel <signin@example.com>",
                               transport=httpx.MockTransport(recorder), **kw)


def sample_message(settings) -> mailing.Message:
    """The sign in email for link 42."""
    return mailing.sign_in_message(settings, to="alice@example.com", link="http://localhost:8000/auth/verify?token=abc",
                                  link_id=42)


@pytest.mark.anyio
async def test_resend_request_shape(settings, sleeps):
    recorder = Recorder(200)
    await resend(recorder).send(sample_message(settings))
    (request,) = recorder.requests
    assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_secret_key"
    assert request.headers["idempotency-key"] == "magic-link/42"
    assert request.headers["user-agent"].startswith("sentinel/")
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "from": "Sentinel <signin@example.com>",
        "to": ["alice@example.com"],
        "subject": "Sign in to Sentinel",
        "text": sample_message(settings).text,
        "html": sample_message(settings).html,
    }
    assert sleeps == []


@pytest.mark.anyio
@pytest.mark.parametrize("first", [httpx.ConnectError, httpx.ReadTimeout, 429, 500, 503])
async def test_resend_retries_then_succeeds(settings, sleeps, first):
    recorder = Recorder(first, first, 200)
    await resend(recorder).send(sample_message(settings))
    assert len(recorder.requests) == 3
    assert sleeps == [0.5, 1.0]
    assert {r.headers["idempotency-key"] for r in recorder.requests} == {"magic-link/42"}


@pytest.mark.anyio
async def test_resend_gives_up_after_the_last_retry_naming_only_the_status(settings, sleeps):
    recorder = Recorder(502)
    with pytest.raises(mailing.MailError) as info:
        await resend(recorder).send(sample_message(settings))
    assert len(recorder.requests) == 3
    assert str(info.value) == "Resend answered 502"
    assert info.value.__cause__ is None and info.value.__context__ is None


@pytest.mark.anyio
async def test_resend_network_failure_names_no_detail(settings, sleeps):
    recorder = Recorder(httpx.ConnectError)
    with pytest.raises(mailing.MailError) as info:
        await resend(recorder, retries=1).send(sample_message(settings))
    assert len(recorder.requests) == 2
    assert "network error" in str(info.value)
    assert "re_secret_key" not in str(info.value) and "boom" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_resend_does_not_retry_client_errors(settings, sleeps, status):
    recorder = Recorder(status)
    with pytest.raises(mailing.MailError) as info:
        await resend(recorder).send(sample_message(settings))
    assert len(recorder.requests) == 1 and sleeps == []
    assert str(info.value) == f"Resend answered {status}"
    assert "secret detail" not in str(info.value) and "re_secret_key" not in str(info.value)


REFUSAL_CASES = [
    (403, "validation_error", "You can only send testing emails to your own email address.",
     "the resend.dev test sender only delivers to the email that owns the Resend account"),
    (403, "validation_error", "The sentinel.example domain is not verified. Please verify it.",
     "the MAIL_FROM domain is not verified in Resend"),
    (403, "restricted_api_key", "API key is not active", "the Resend API key is not active"),
    (403, "suspended_api_key", "This API key is suspended", "the Resend API key is suspended"),
    (401, "validation_error", "API key is invalid", "the Resend API key is invalid"),
    (401, "restricted_api_key", "This API key is restricted to only send emails.", "the Resend API key is restricted"),
]


def resend_answering(handler) -> mailing.ResendMailer:
    """A ResendMailer whose transport answers every request with handler."""
    return mailing.ResendMailer("re_secret_key", "Sentinel <x@y.example>", transport=httpx.MockTransport(handler))


@pytest.mark.anyio
@pytest.mark.parametrize("status, name, message, reason", REFUSAL_CASES)
async def test_a_known_resend_refusal_is_named_in_our_own_words(settings, sleeps, status, name, message, reason):
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"name": name, "message": message})

    mailer_under_test = resend_answering(refuse)
    with pytest.raises(mailing.MailError) as info:
        await mailer_under_test.send(sample_message(settings))
    assert str(info.value) == f"Resend answered {status}: {reason}"


@pytest.mark.anyio
async def test_an_unknown_refusal_names_only_the_short_error_code_and_never_resends_text(settings, sleeps):
    def refuse(request: httpx.Request) -> httpx.Response:
        body = {"name": "validation_error", "message": "Invalid `to` field: leaked@person.example"}
        return httpx.Response(422, json=body)

    mailer_under_test = resend_answering(refuse)
    with pytest.raises(mailing.MailError) as info:
        await mailer_under_test.send(sample_message(settings))
    assert str(info.value) == "Resend answered 422: validation_error"
    assert "leaked@person.example" not in str(info.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload", [{"name": "Not A Code!!", "message": "x"}, {"name": 7}, ["not", "a", "dict"], "plain text"]
)
async def test_an_odd_refusal_body_adds_nothing_to_the_error(settings, sleeps, payload):
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=payload)

    mailer_under_test = resend_answering(refuse)
    with pytest.raises(mailing.MailError) as info:
        await mailer_under_test.send(sample_message(settings))
    assert str(info.value) == "Resend answered 403"


def test_settings_repr_hides_the_secrets(https_settings):
    text = repr(https_settings) + str(https_settings)
    assert https_settings.session_secret.decode() not in text
    assert https_settings.token_hash_secret.decode() not in text
    assert https_settings.resend_api_key and https_settings.resend_api_key not in text


def test_resend_repr_hides_the_key():
    assert "re_secret_key" not in repr(mailing.ResendMailer("re_secret_key", "a@example.com"))


@pytest.mark.anyio
async def test_console_mailer_prints_the_link(settings):
    stream = io.StringIO()
    await mailing.ConsoleMailer(stream).send(sample_message(settings))
    assert stream.getvalue() == "Sign in link for alice@example.com: http://localhost:8000/auth/verify?token=abc\n"


@pytest.mark.anyio
async def test_console_mailer_defaults_to_stderr(settings, capsys):
    await mailing.ConsoleMailer().send(sample_message(settings))
    assert "http://localhost:8000/auth/verify?token=abc" in capsys.readouterr().err


def test_sign_in_message(settings):
    message = sample_message(settings)
    assert message.to == "alice@example.com" and message.subject == "Sign in to Sentinel"
    assert message.idempotency_key == "magic-link/42"
    for body in (message.text, message.html):
        assert "15 minutes" in body and "once" in body
        assert "If you did not ask to sign in, ignore this email." in body
    assert "http://localhost:8000/auth/verify?token=abc" in message.text


def test_sign_in_message_escapes_html(settings):
    link = 'http://localhost:8000/auth/verify?token=a&b="><script>x</script>'
    message = mailing.sign_in_message(settings, to="a@example.com", link=link, link_id=1)
    assert "<script>" not in message.html
    assert "&amp;b=&quot;&gt;&lt;script&gt;" in message.html
    assert link in message.text


def test_make_mailer_picks_the_right_class(settings, https_settings):
    assert isinstance(mailing.make_mailer(settings), mailing.ConsoleMailer)
    assert isinstance(mailing.make_mailer(https_settings), mailing.ResendMailer)
    keyed = load_settings(dev_env(RESEND_API_KEY="re_test_key", MAIL_FROM="a@example.com"), "127.0.0.1")
    assert isinstance(mailing.make_mailer(keyed), mailing.ResendMailer)
    unsafe = Settings(base_url="https://x.example", session_secret=b"a" * 32, token_hash_secret=b"b" * 32,
                      resend_api_key=None, mail_from=None, bind_host="0.0.0.0", dev_mode=False)
    with pytest.raises(ValueError):
        mailing.make_mailer(unsafe)


# Over HTTP: the whole app (web/app.py) through conftest's browser() clients

COOKIE = "sentinel_session"
EVIL = "http://evil.example"
HTTPS_BASE = "https://sentinel.example.com"
SITE_CSP = (
    f"default-src 'none'; script-src {web_app.TAILWIND}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'none'; object-src 'none'; form-action 'self'; frame-ancestors 'none'"
)


def set_cookie_header(response: httpx.Response, name: str = COOKIE) -> str:
    """The Set-Cookie header of a response that sets (or clears) this cookie."""
    found = [value for value in response.headers.get_list("set-cookie") if value.startswith(name + "=")]
    assert len(found) == 1, response.headers.get_list("set-cookie")
    return found[0]


def cookie_flags(header: str) -> set[str]:
    """The attributes of a Set-Cookie header, lowercased, values kept ("path=/", "samesite=lax", "httponly")."""
    return {part.strip().lower() for part in header.split(";")[1:]}


def clears_cookie(response: httpx.Response, name: str = COOKIE) -> bool:
    """True when the response expires this cookie."""
    flags = cookie_flags(set_cookie_header(response, name))
    return "max-age=0" in flags or any(f.startswith("expires=") and "1970" in f for f in flags)


def link_rows(conn) -> list[dict[str, Any]]:
    """Every magic link in this test's transaction, oldest first."""
    return conn.execute("SELECT * FROM magic_links ORDER BY id").fetchall()


def user_row(conn, email_key: str) -> dict[str, Any] | None:
    """The users row with this email key, or None."""
    return conn.execute(
        "SELECT id::text AS id, email, email_key, session_epoch, wallet_address, agent_id FROM users"
        " WHERE email_key = %s",
        (email_key,),
    ).fetchone()


def user_id_of(client: httpx.AsyncClient, name: str = COOKIE) -> str:
    """The user id in a browser's session cookie ("v2.<user id>.<epoch>.<expires>.<nonce>.<signature>")."""
    value = client.cookies.get(name)
    assert value, "the browser has no session cookie"
    return value.split(".")[1]


def set_session_cookie(client: httpx.AsyncClient, value: str) -> None:
    """Replace the browser's session cookie with this value."""
    client.cookies.clear()
    client.cookies.set(COOKIE, value)


async def account_csrf(client: httpx.AsyncClient) -> str:
    """The account page's CSRF token for this browser's session."""
    page = await client.get("/account")
    assert page.status_code == 200, page.text
    return form_value(page.text, "csrf")


async def signed_in_browser(browser, mailer, email: str, ip: str = BROWSER_IP) -> httpx.AsyncClient:
    """A new browser signed in as this address."""
    client = browser(ip)
    await sign_in(client, mailer, email)
    return client


@asynccontextmanager
async def serve(settings: Settings, **kw: Any) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """A second app built from these settings (create_app keywords in kw), with its lifespan, and a browser factory
    for it (each browser sends Origin: settings.base_url, follows no redirects)."""
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



async def post_with_origin(
    client: httpx.AsyncClient, path: str, data: dict[str, str], origin: str | None, **headers: str
) -> httpx.Response:
    """POST a form with this Origin header, or with none at all when origin is None (cookies still sent)."""
    request = client.build_request("POST", path, data=data, headers=headers)
    if origin is None:
        del request.headers["origin"]
    else:
        request.headers["origin"] = origin
    return await client.send(request)


async def mcp_status(client: httpx.AsyncClient, access_token: str) -> int:
    """The status /mcp answers a tools/list with this bearer token."""
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json, text/event-stream"},
    )
    return response.status_code


# Links over HTTP


@pytest.mark.anyio
async def test_http_link_is_stored_only_as_a_hash_and_logs_hold_no_token_or_email(
    browser, mailer, conn, settings, caplog
):
    caplog.set_level(logging.DEBUG)
    client = browser()
    asked = await client.post("/signin", data={"email": "Alice@Example.com"})
    assert asked.status_code == 200
    token = mailer.last_token()
    (row,) = link_rows(conn)
    assert row["token_hash"] == auth.digest(settings.key("magic-link"), token)
    assert all(token not in str(value) for value in row.values())
    assert row["ip_hash"] == rl.ip_hash(settings, BROWSER_IP)
    assert (await client.post("/auth/verify", data={"token": token})).status_code == 303
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged and "alice@example.com" not in logged.lower()


@pytest.mark.anyio
async def test_get_verify_consumes_nothing(browser, mailer, conn):
    client = browser()
    await client.post("/signin", data={"email": "Alice@Example.com", "next": "/account"})
    token = mailer.last_token()
    for _ in range(2):
        page = await client.get("/auth/verify", params={"token": token})
        assert page.status_code == 200
        assert "Alice@example.com" in page.text and form_value(page.text, "token") == token
        assert "set-cookie" not in page.headers
    (row,) = link_rows(conn)
    assert row["used_at"] is None
    assert user_row(conn, "alice@example.com") is None
    assert (await client.post("/auth/verify", data={"token": token})).status_code == 303


@pytest.mark.anyio
async def test_post_verify_signs_in_and_creates_the_user_and_wallet(browser, mailer, conn, fake_wallets):
    client = browser()
    response = await sign_in(client, mailer, "Alice@Example.com")
    assert response.headers["location"] == "/account"
    user = user_row(conn, "alice@example.com")
    assert user is not None and user["email"] == "Alice@example.com"
    assert user_id_of(client) == user["id"]
    assert fake_wallets.opened == [(user["id"], True)]
    assert user["wallet_address"] == fake_wallets.address_for(user["id"]) and user["agent_id"] is not None
    account = await client.get("/account")
    assert account.status_code == 200
    assert "Alice@example.com" in account.text and user["wallet_address"] in account.text

    again = browser("198.51.100.20")
    await sign_in(again, mailer, "alice@EXAMPLE.com")  # the same user; the wallet exists, so CDP is not called
    assert user_id_of(again) == user["id"]
    assert fake_wallets.opened == [(user["id"], True)]
    assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


@pytest.mark.anyio
async def test_http_link_is_single_use(browser, mailer, conn):
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    token = mailer.last_token()
    assert (await client.post("/auth/verify", data={"token": token})).status_code == 400
    other = browser("198.51.100.20")
    assert (await other.post("/auth/verify", data={"token": token})).status_code == 400
    assert (await other.get("/auth/verify", params={"token": token})).status_code == 400
    assert COOKIE not in other.cookies


@pytest.mark.anyio
async def test_http_link_expires_after_15_minutes(browser, mailer, conn):
    client = browser()
    await client.post("/signin", data={"email": "alice@example.com"})
    token = mailer.last_token()
    age_links(conn, 14)
    assert (await client.get("/auth/verify", params={"token": token})).status_code == 200
    age_links(conn, 2)
    assert (await client.get("/auth/verify", params={"token": token})).status_code == 400
    refused = await client.post("/auth/verify", data={"token": token})
    assert refused.status_code == 400 and "set-cookie" not in refused.headers
    assert user_row(conn, "alice@example.com") is None


@pytest.mark.anyio
async def test_http_tampered_tokens_are_refused(browser, mailer, conn):
    client = browser()
    await client.post("/signin", data={"email": "alice@example.com"})
    token = mailer.last_token()
    flipped = ("A" if token[0] != "A" else "B") + token[1:]
    for bad in [flipped, token[:-1], token + "x", "", "!" * 43]:
        assert (await client.get("/auth/verify", params={"token": bad})).status_code == 400
        assert (await client.post("/auth/verify", data={"token": bad})).status_code == 400
    assert (await client.post("/auth/verify")).status_code == 400  # no token field at all
    assert link_rows(conn)[0]["used_at"] is None and user_row(conn, "alice@example.com") is None
    assert COOKIE not in client.cookies
    assert (await client.post("/auth/verify", data={"token": token})).status_code == 303


# Rate limits over HTTP: 429 with Retry-After, no mail, no row


def assert_rate_limited(response: httpx.Response) -> int:
    """Check a 429 page and return its Retry-After in seconds."""
    assert response.status_code == 429, response.text
    assert "Too many sign in links" in response.text
    return int(response.headers["retry-after"])


@pytest.mark.anyio
async def test_http_three_links_per_address_including_tag_variants(browser, mailer, conn):
    for n, email in enumerate(["alice@example.com", "Alice+one@example.com", "alice+two@example.com"]):
        assert (await browser(f"198.51.100.{n + 1}").post("/signin", data={"email": email})).status_code == 200
    refused = await browser("192.0.2.99").post("/signin", data={"email": "ALICE+three@example.com"})
    assert assert_rate_limited(refused) == 15 * 60
    assert len(mailer.messages) == 3 and len(link_rows(conn)) == 3
    assert (await browser("192.0.2.99").post("/signin", data={"email": "bob@example.com"})).status_code == 200


@pytest.mark.anyio
async def test_http_links_per_ip_count_ipv4_mapped_as_ipv4(browser, mailer, conn, settings):
    fill_links(conn, 59, ip_hash=rl.ip_hash(settings, "198.51.100.7"))
    sixtieth = await browser("::ffff:198.51.100.7").post("/signin", data={"email": "frank@example.com"})
    assert sixtieth.status_code == 200
    refused = await browser("198.51.100.7").post("/signin", data={"email": "grace@example.com"})
    assert 1 <= assert_rate_limited(refused) <= 3600
    assert len(mailer.messages) == 1 and len(link_rows(conn)) == 60
    assert (await browser("198.51.100.8").post("/signin", data={"email": "grace@example.com"})).status_code == 200


@pytest.mark.anyio
async def test_http_links_per_ip_group_ipv6_by_64(browser, mailer, conn, settings):
    fill_links(conn, 60, ip_hash=rl.ip_hash(settings, "2001:db8:1:2::1"))
    refused = await browser("2001:db8:1:2:aaaa:bbbb:cccc:dddd").post("/signin", data={"email": "heidi@example.com"})
    assert_rate_limited(refused)
    assert mailer.messages == [] and len(link_rows(conn)) == 60
    allowed = await browser("2001:db8:1:3::1").post("/signin", data={"email": "heidi@example.com"})
    assert allowed.status_code == 200


@pytest.mark.anyio
async def test_http_global_link_cap(browser, mailer, conn):
    fill_links(conn, 300)
    refused = await browser("192.0.2.1").post("/signin", data={"email": "judy@example.com"})
    assert assert_rate_limited(refused) == 3600
    assert mailer.messages == [] and len(link_rows(conn)) == 300


# Mail and the check email page


@pytest.mark.anyio
async def test_check_email_page_echoes_the_typed_address_and_is_the_same_for_known_and_unknown(browser, mailer, conn):
    users.find_or_create_user(conn, "Bob@example.com", "bob@example.com")
    known = await browser("198.51.100.1").post("/signin", data={"email": "  BOB@Example.com "})
    unknown = await browser("198.51.100.2").post("/signin", data={"email": "  ZED@Example.com "})
    assert known.status_code == unknown.status_code == 200
    assert "BOB@Example.com" in known.text and "ZED@Example.com" in unknown.text
    assert known.text.replace("BOB", "X") == unknown.text.replace("ZED", "X")
    assert sorted(known.headers.keys()) == sorted(unknown.headers.keys())
    assert "set-cookie" not in known.headers
    assert [m.to for m in mailer.messages] == ["Bob@example.com", "ZED@example.com"]


@pytest.mark.anyio
async def test_mail_goes_to_the_stored_address_for_existing_users(browser, mailer, conn):
    users.find_or_create_user(conn, "Carol.Smith@example.com", "carol.smith@example.com")
    client = browser()
    page = await client.post("/signin", data={"email": "CAROL.SMITH@EXAMPLE.COM"})
    assert "CAROL.SMITH@EXAMPLE.COM" in page.text
    assert mailer.messages[-1].to == "Carol.Smith@example.com"
    assert link_rows(conn)[-1]["email_key"] == "carol.smith@example.com"
    await client.post("/auth/verify", data={"token": mailer.last_token()})
    assert user_row(conn, "carol.smith@example.com")["email"] == "Carol.Smith@example.com"  # unchanged


@pytest.mark.anyio
async def test_new_addresses_are_normalized_before_mailing(browser, mailer, conn):
    client = browser()
    await sign_in(client, mailer, " Dan@Bücher.DE ")
    assert mailer.messages[-1].to == "Dan@xn--bcher-kva.de"
    assert user_row(conn, "dan@xn--bcher-kva.de")["email"] == "Dan@xn--bcher-kva.de"


@pytest.mark.anyio
@pytest.mark.parametrize("typed", ["", "not-an-email", "a" * 250 + "@example.com"])
async def test_invalid_addresses_get_the_form_back_and_nothing_is_stored(browser, mailer, conn, typed):
    page = await browser().post("/signin", data={"email": typed})
    assert page.status_code == 400 and 'name="email"' in page.text
    assert mailer.messages == [] and link_rows(conn) == []


@pytest.mark.anyio
async def test_mail_failure_answers_502(browser, mailer):
    mailer.fail = True
    page = await browser().post("/signin", data={"email": "alice@example.com"})
    assert page.status_code == 502 and "could not be sent" in page.text


# The next allow list, end to end


@pytest.mark.anyio
@pytest.mark.parametrize("next_path", ["/account", "/oauth/consent?req=abc.DEF_12-3"])
async def test_next_on_the_allow_list_is_followed_after_sign_in(browser, mailer, conn, next_path):
    client = browser()
    form = await client.get("/signin", params={"next": next_path})
    assert form_value(form.text, "next") == next_path
    response = await sign_in(client, mailer, "alice@example.com", next=next_path)
    assert response.headers["location"] == next_path
    assert link_rows(conn)[-1]["next_path"] == next_path
    already = await client.get("/signin", params={"next": next_path})  # signed in: straight on
    assert already.status_code == 303 and already.headers["location"] == next_path


@pytest.mark.anyio
@pytest.mark.parametrize("next_path", ["//evil.com", "https://evil.com", "/signin", "/account?x=1", "/\\evil.com"])
async def test_a_bad_next_falls_back_to_the_account_page(browser, mailer, conn, next_path):
    client = browser()
    form = await client.get("/signin", params={"next": next_path})
    assert 'name="next"' not in form.text
    response = await sign_in(client, mailer, "alice@example.com", next=next_path)
    assert response.headers["location"] == "/account"
    assert link_rows(conn)[-1]["next_path"] is None
    already = await client.get("/signin", params={"next": next_path})
    assert already.headers["location"] == "/account"


# Sessions over HTTP


@pytest.mark.anyio
async def test_session_cookie_flags_on_http(browser, mailer):
    response = await sign_in(browser(), mailer, "alice@example.com")
    flags = cookie_flags(set_cookie_header(response))
    assert {"httponly", "samesite=lax", "path=/", f"max-age={auth.SESSION_TTL_S}"} <= flags
    assert "secure" not in flags and not any(f.startswith("domain=") for f in flags)
    assert "__Host-" not in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_https_cookie_has_the_host_prefix_and_secure_and_pages_get_hsts(
    https_settings, connect, mailer, fake_wallets, pay_spy
):
    async with serve(https_settings, connect=connect, mailer=mailer, pay_fn=pay_spy) as make:
        client = make()
        assert client.base_url.scheme == "https"
        await client.post("/signin", data={"email": "alice@example.com"})
        assert mailer.messages[-1].text.count(HTTPS_BASE + "/auth/verify?token=") == 1
        response = await client.post("/auth/verify", data={"token": mailer.last_token()})
        assert response.status_code == 303
        name = "__Host-sentinel_session"
        flags = cookie_flags(set_cookie_header(response, name))
        assert {"secure", "httponly", "samesite=lax", "path=/"} <= flags
        assert not any(f.startswith("domain=") for f in flags)
        assert (await client.get("/account")).status_code == 200
        landing = await client.get("/")
        assert landing.headers["strict-transport-security"] == "max-age=31536000"
        assert landing.headers["content-security-policy"] == SITE_CSP
        refused = await post_with_origin(client, "/signin", {"email": "a@example.com"}, TEST_BASE_URL)
        assert refused.status_code == 403  # the http origin is foreign to the https site


@pytest.mark.anyio
async def test_tampered_session_cookies_are_signed_out(browser, mailer, settings):
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    value = client.cookies[COOKIE]
    version, user_id, epoch, expires, nonce, signature = value.split(".")
    middle = len(signature) // 2
    bad_signature = signature[:middle] + ("A" if signature[middle] != "A" else "B") + signature[middle + 1:]
    for bad in [
        f"{version}.{UUID}.{epoch}.{expires}.{nonce}.{signature}",
        f"{version}.{user_id}.{int(epoch) + 1}.{expires}.{nonce}.{signature}",
        f"{version}.{user_id}.{epoch}.{int(expires) + 999}.{nonce}.{signature}",
        f"{version}.{user_id}.{epoch}.{expires}.{nonce}.{bad_signature}",
        auth.make_session(settings, UUID, 0),  # signed, but no such user
        "garbage",
    ]:
        set_session_cookie(client, bad)
        account = await client.get("/account")
        assert account.status_code == 303 and account.headers["location"] == "/signin?next=%2Faccount"
        assert clears_cookie(account)
        set_session_cookie(client, bad)
        landing = await client.get("/")
        assert landing.status_code == 200 and clears_cookie(landing)
    set_session_cookie(client, value)
    assert (await client.get("/account")).status_code == 200


@pytest.mark.anyio
async def test_expired_session_cookies_are_signed_out(browser, mailer, monkeypatch):
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    now = time.time()
    monkeypatch.setattr(auth, "clock", lambda: now + auth.SESSION_TTL_S - 60)
    assert (await client.get("/account")).status_code == 200
    monkeypatch.setattr(auth, "clock", lambda: now + auth.SESSION_TTL_S + 60)
    account = await client.get("/account")
    assert account.status_code == 303 and clears_cookie(account)


@pytest.mark.anyio
async def test_sign_out_everywhere_ends_other_sessions_and_revokes_grants(browser, mailer, conn, settings):
    first = await signed_in_browser(browser, mailer, "erin@example.com", "198.51.100.1")
    second = await signed_in_browser(browser, mailer, "erin@example.com", "198.51.100.2")
    bystander = await signed_in_browser(browser, mailer, "frank@example.com", "198.51.100.3")
    uid = user_id_of(first)
    identity = users.identity_for(conn, uid)
    assert identity is not None
    token = access_token_for(conn, settings, identity)
    other_identity = users.identity_for(conn, user_id_of(bystander))
    other_token = access_token_for(conn, settings, other_identity)
    assert await mcp_status(first, token) != 401
    epoch = user_row(conn, "erin@example.com")["session_epoch"]
    old_cookie = first.cookies[COOKIE]

    response = await first.post("/account/signout-everywhere", data={"csrf": await account_csrf(first)})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert clears_cookie(response)
    assert user_row(conn, "erin@example.com")["session_epoch"] == epoch + 1

    elsewhere = await second.get("/account")
    assert elsewhere.status_code == 303 and clears_cookie(elsewhere)
    set_session_cookie(first, old_cookie)
    assert (await first.get("/account")).status_code == 303
    grants = conn.execute("SELECT revoked_at FROM oauth_codes WHERE user_id = %s", (uid,)).fetchall()
    assert grants and all(g["revoked_at"] is not None for g in grants)
    assert await mcp_status(first, token) == 401

    assert (await bystander.get("/account")).status_code == 200  # other users are untouched
    assert await mcp_status(bystander, other_token) != 401


@pytest.mark.anyio
async def test_sign_out_clears_the_cookie(browser, mailer):
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    csrf = await account_csrf(client)
    refused = await client.post("/signout")  # signed in: the CSRF token is required
    assert refused.status_code == 403 and (await client.get("/account")).status_code == 200
    response = await client.post("/signout", data={"csrf": csrf})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert clears_cookie(response)
    assert COOKIE not in client.cookies
    assert (await client.get("/account")).status_code == 303
    signed_out = await client.post("/signout")  # no session: nothing to protect, still fine
    assert signed_out.status_code == 303


# Origin and CSRF on site forms


@pytest.mark.anyio
@pytest.mark.parametrize("origin", [EVIL, "null", None, TEST_BASE_URL + "/", "https://localhost:8000"])
async def test_every_site_post_refuses_a_foreign_null_or_missing_origin(
    browser, mailer, conn, fake_wallets, origin
):
    client = browser()
    await sign_in(client, mailer, "alice@example.com")
    csrf = await account_csrf(client)
    await client.post("/signin", data={"email": "alice@example.com"})
    fresh = mailer.last_token()
    before = (len(mailer.messages), len(link_rows(conn)), user_row(conn, "alice@example.com")["session_epoch"],
              list(fake_wallets.opened))
    forms = [
        ("/signin", {"email": "someone@example.com"}),
        ("/auth/verify", {"token": fresh}),
        ("/account/wallet", {"csrf": csrf, "next": "/account"}),
        ("/account/signout-everywhere", {"csrf": csrf}),
        ("/signout", {"csrf": csrf}),
        ("/oauth/consent", {"req": "x", "csrf": csrf, "action": "allow"}),
    ]
    for path, data in forms:
        response = await post_with_origin(client, path, data, origin)
        assert response.status_code == 403, (path, response.status_code)
        assert "set-cookie" not in response.headers
    after = (len(mailer.messages), len(link_rows(conn)), user_row(conn, "alice@example.com")["session_epoch"],
             list(fake_wallets.opened))
    assert after == before
    assert link_rows(conn)[-1]["used_at"] is None
    assert (await client.get("/account")).status_code == 200


@pytest.mark.anyio
async def test_sec_fetch_site_same_origin_stands_in_for_a_missing_origin(browser, mailer):
    client = browser()
    allowed = await post_with_origin(client, "/signin", {"email": "alice@example.com"}, None,
                                     **{"sec-fetch-site": "same-origin"})
    assert allowed.status_code == 200 and len(mailer.messages) == 1
    refused = await post_with_origin(client, "/signin", {"email": "alice@example.com"}, None,
                                     **{"sec-fetch-site": "same-site"})
    assert refused.status_code == 403 and len(mailer.messages) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/account/wallet", "/account/signout-everywhere", "/signout"])
async def test_account_forms_refuse_a_missing_or_foreign_csrf_token(
    browser, mailer, conn, settings, monkeypatch, path
):
    alice = await signed_in_browser(browser, mailer, "alice@example.com", "198.51.100.1")
    bob = await signed_in_browser(browser, mailer, "bob@example.com", "198.51.100.2")
    # Another session of the same user. Sessions carry no random part, so two sign ins in the same second get the
    # same cookie (and the same CSRF token); a later second makes it a different session.
    later = time.time() + 5
    monkeypatch.setattr(auth, "clock", lambda: later)
    alice_again = await signed_in_browser(browser, mailer, "alice@example.com", "198.51.100.3")
    assert alice_again.cookies[COOKIE] != alice.cookies[COOKIE]
    epoch = user_row(conn, "alice@example.com")["session_epoch"]
    for csrf in [None, "", await account_csrf(bob), await account_csrf(alice_again),
                 auth.csrf_token(settings, alice.cookies[COOKIE], "consent:abc")]:
        data = {} if csrf is None else {"csrf": csrf}
        response = await alice.post(path, data=data)
        assert response.status_code == 403, (csrf, response.status_code)
    assert user_row(conn, "alice@example.com")["session_epoch"] == epoch
    assert (await alice.get("/account")).status_code == 200


@pytest.mark.anyio
async def test_account_forms_send_a_signed_out_browser_to_sign_in(browser):
    client = browser()
    for path in ["/account/wallet", "/account/signout-everywhere"]:
        response = await client.post(path, data={"csrf": "x"})
        assert response.status_code == 303 and response.headers["location"] == "/signin?next=%2Faccount"


# Wallet retry


@pytest.mark.anyio
async def test_wallet_retry_after_a_failed_first_sign_in(browser, mailer, conn, fake_wallets):
    fake_wallets.fail = True
    client = browser()
    response = await sign_in(client, mailer, "alice@example.com")
    assert response.headers["location"] == "/account" and COOKIE in client.cookies  # signed in anyway
    uid = user_id_of(client)
    assert user_row(conn, "alice@example.com")["wallet_address"] is None
    account = await client.get("/account")
    assert account.status_code == 200 and 'action="/account/wallet"' in account.text
    csrf = form_value(account.text, "csrf")

    still_failing = await client.post("/account/wallet", data={"csrf": csrf, "next": "/account"})
    assert still_failing.status_code == 502
    assert user_row(conn, "alice@example.com")["wallet_address"] is None

    fake_wallets.fail = False
    retried = await client.post("/account/wallet", data={"csrf": csrf, "next": "//evil.com"})
    assert retried.status_code == 303 and retried.headers["location"] == "/account"
    address = fake_wallets.address_for(uid)
    assert user_row(conn, "alice@example.com")["wallet_address"] == address
    assert fake_wallets.opened == [(uid, True)] * 3
    assert address in (await client.get("/account")).text
    again = await client.post("/account/wallet", data={"csrf": csrf, "next": "/account"})
    assert again.status_code == 303 and len(fake_wallets.opened) == 3  # the stored wallet is reused


# Security headers


def assert_site_headers(response: httpx.Response) -> None:
    """The site's security headers, exactly, and no HSTS on http."""
    assert response.headers["content-security-policy"] == SITE_CSP, response.request.url
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers


@pytest.mark.anyio
async def test_security_headers_on_site_pages_and_authorize_but_not_mcp_or_token(browser, mailer):
    client = browser()
    for path in ["/", "/signin", "/auth/verify?token=nope", "/account", "/healthz", "/authorize",
                 "/authorize?client_id=nope&response_type=code", "/oauth/consent?req=nope"]:
        assert_site_headers(await client.get(path))
    assert_site_headers(await client.post("/signin", data={"email": "alice@example.com"}))
    assert_site_headers(await client.post("/signin", headers={"origin": EVIL}))
    signed_in = await client.post("/auth/verify", data={"token": mailer.last_token()})
    assert_site_headers(signed_in)
    assert_site_headers(await client.get("/account"))

    for response in [
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        await client.post("/token", data={"grant_type": "authorization_code"}),
    ]:
        assert response.status_code in (400, 401), response.status_code
        for name in ["content-security-policy", "x-frame-options", "referrer-policy", "strict-transport-security"]:
            assert name not in response.headers, (response.request.url, name)


# Infrastructure: the access log and the console mailer


@pytest.fixture
def uvicorn_loggers() -> Any:
    """Put the uvicorn loggers back as they were after a test that runs uvicorn's logging dictConfig."""
    names = ["uvicorn", "uvicorn.error", "uvicorn.access"]
    saved = {
        name: (list(lg.handlers), list(lg.filters), lg.level, lg.propagate, lg.disabled)
        for name, lg in ((name, logging.getLogger(name)) for name in names)
    }
    yield
    for name, (handlers, filters, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.filters[:] = filters
        lg.setLevel(level)
        lg.propagate = propagate
        lg.disabled = disabled


class ListHandler(logging.Handler):
    """Keeps every formatted message."""

    def __init__(self) -> None:
        """No messages yet."""
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the message with its arguments filled in."""
        self.messages.append(record.getMessage())


def test_strip_query_survives_uvicorn_log_config(uvicorn_loggers, capsys):
    attached = web_app.attach_strip_query()
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # what uvicorn.run does at startup
    access = logging.getLogger("uvicorn.access")
    assert [f for f in access.filters if isinstance(f, web_app.StripQuery)] == [attached]
    assert web_app.attach_strip_query() is attached and len(access.filters) == len(set(access.filters))
    seen = ListHandler()
    access.addHandler(seen)
    fmt = '%s - "%s %s HTTP/%s" %d'
    access.info(fmt, "203.0.113.10:50000", "GET", "/auth/verify?token=SECRETTOKEN", "1.1", 200)
    access.info(fmt, "203.0.113.10:50000", "GET", "/oauth/consent?req=PENDINGBLOB", "1.1", 200)
    access.info(fmt, "203.0.113.10:50000", "GET", "/account", "1.1", 200)
    assert seen.messages == [
        '203.0.113.10:50000 - "GET /auth/verify HTTP/1.1" 200',
        '203.0.113.10:50000 - "GET /oauth/consent HTTP/1.1" 200',
        '203.0.113.10:50000 - "GET /account HTTP/1.1" 200',
    ]
    printed = capsys.readouterr()
    assert "SECRETTOKEN" not in printed.out + printed.err and "PENDINGBLOB" not in printed.out + printed.err


@pytest.mark.anyio
async def test_dev_app_without_a_mailer_prints_the_link_to_stderr(settings, connect, fake_wallets, pay_spy, capsys):
    async with serve(settings, connect=connect, mailer=None, pay_fn=pay_spy) as make:
        client = make()
        asked = await client.post("/signin", data={"email": "Alice@Example.com"})
        assert asked.status_code == 200
        err = capsys.readouterr().err
        prefix = f"Sign in link for Alice@example.com: {TEST_BASE_URL}/auth/verify?token="
        assert prefix in err
        token = err.split(prefix, 1)[1].split()[0]
        assert auth.TOKEN_RE.fullmatch(token)
        assert (await client.post("/auth/verify", data={"token": token})).status_code == 303


# Opening the site at 127.0.0.1 instead of localhost (a 403 on every form before LocalhostAlias)


def alias_client(app, host: str = "http://127.0.0.1:8000") -> httpx.AsyncClient:
    """A client that reaches the app under another host name, as a browser opened at that address would."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.77", 50000)), base_url=host, follow_redirects=False,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["http://127.0.0.1:8000", "http://[::1]:8000"])
async def test_a_page_opened_at_a_loopback_alias_is_sent_to_the_configured_address(app, host):
    async with alias_client(app, host) as client:
        for path, expected in [("/signin", "/signin"), ("/signin?next=/account", "/signin?next=/account"),
                               ("/account/rules", "/account/rules"), ("/", "/")]:
            response = await client.get(path)
            assert response.status_code == 307, path
            assert response.headers["location"] == TEST_BASE_URL + expected
        head = await client.head("/signin")
        assert head.status_code == 307


@pytest.mark.anyio
async def test_the_alias_redirect_leaves_health_mcp_other_hosts_ports_and_posts_alone(app):
    async with alias_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/mcp", content=b"{}")).status_code == 401  # the MCP challenge, not a redirect
        posted = await client.post("/signin", data={"email": "alias@example.com"}, headers={"origin": "http://127.0.0.1:8000"})
        assert posted.status_code == 403 and TEST_BASE_URL in posted.text  # refused, and it says where to go
    async with alias_client(app, "http://127.0.0.1:9000") as other_port:
        assert (await other_port.get("/signin")).status_code == 200
    async with alias_client(app, "http://evil.example:8000") as stranger:
        assert (await stranger.get("/signin")).status_code == 200  # never a redirect to or from a stranger's name


@pytest.mark.anyio
async def test_the_alias_redirect_is_off_outside_localhost(https_settings, connect, mailer):
    async with serve(https_settings, connect=connect, mailer=mailer) as make:
        client = make()
        client.base_url = httpx.URL("https://127.0.0.1:8000")  # another name for the same machine, no redirect
        assert (await client.get("/signin")).status_code == 200
