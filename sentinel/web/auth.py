"""Sign in primitives for the site: hashing, email addresses, magic links, session cookies, CSRF and origin checks.

app.py strings these together for /signin, /auth/verify and every form; oauth.py reuses digest and b64. Nothing here
holds state: magic links live in the magic_links table (only an HMAC of each token is stored, under the
"magic-link" subkey), and a session is a signed cookie "v2.<user_id>.<epoch>.<expires>.<nonce>.<signature>" whose
epoch the caller checks against users.session_epoch, so "sign out everywhere" ends every session at once. The random
nonce makes every sign in a different cookie (and so different CSRF tokens), even twice in one second.

Every expiry that matters for links is decided by SQL now(); the session expiry uses the module level clock, which
tests patch. Nothing here logs: links, tokens, cookies and CSRF values must never reach a log.
"""
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from email_validator import EmailNotValidError, validate_email

from sentinel.db import Connection
from sentinel.web import Settings

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32): links and codes; use fullmatch ($ allows "\\n")
LINK_TTL = "15 minutes"  # SQL interval text
SESSION_TTL_S = 24 * 3600
SESSION_VERSION = "v2"  # v1 cookies had no nonce and are refused
SESSION_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16}")  # token_urlsafe(12)
MAX_SESSION = 512  # longer cookie values are rejected before any HMAC work
MAX_EMAIL = 254
NEXT_RE = re.compile(
    r"/account(?:/live|/payments|/rules|/books|/tasks|/texting)?|/oauth/consent\?req=[A-Za-z0-9._-]{1,4096}"
)
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

clock = time.time  # tests monkeypatch it to move session expiry


def digest(key: bytes, value: str) -> str:
    """Hex HMAC-SHA256 of a value: how every link, code, token and IP is stored."""
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def b64(data: bytes) -> str:
    """URL safe base64 without padding (cookie and form safe)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(key: bytes, value: str) -> str:
    """b64 HMAC-SHA256 of a value, for signatures carried by the browser."""
    return b64(hmac.new(key, value.encode(), hashlib.sha256).digest())


def _equal(a: str, b: str) -> bool:
    """Constant time string comparison that also accepts non ASCII input (it simply does not match)."""
    return hmac.compare_digest(a.encode(), b.encode())


# Email addresses


def normalize_email(raw: str) -> tuple[str, str]:
    """Return (email, email_key) for an address typed into the sign in form.

    email is the ASCII form (IDNA domain) mail is sent to; email_key is its lowercase form, the lookup key.
    Raises ValueError with a plain English message when the address is invalid or longer than 254 characters.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter your email address.")
    if len(value) > MAX_EMAIL:
        raise ValueError("That email address is too long.")
    try:
        result = validate_email(value, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as exc:
        raise ValueError(f"That email address is not valid. {exc}") from None
    email = result.ascii_email
    if not email or len(email) > MAX_EMAIL:
        raise ValueError("That email address is too long.")
    return email, email.lower()


def limit_key(email_key: str) -> str:
    """The key rate limits count by: the address without its +tag, and for Gmail without dots on gmail.com.

    Only for counting: mail still goes to the address the user typed (or the one on file).
    """
    local, _, domain = email_key.lower().rpartition("@")
    base = local.split("+", 1)[0] or local
    if domain in GMAIL_DOMAINS:
        base = base.replace(".", "") or base
        domain = "gmail.com"
    return f"{base}@{domain}"


def safe_next(raw: str | None) -> str | None:
    """Return where to go after sign in when it is one of the allowed places, else None.

    Only "/account", "/account/live", "/account/payments", "/account/rules", "/account/books", "/account/tasks" or
    "/oauth/consent?req=<value>" with value matching [A-Za-z0-9._-]{1,4096}, so the site can never be used to redirect
    somewhere else.
    """
    if not raw or not NEXT_RE.fullmatch(raw):
        return None
    return raw


# Magic links


def issue_link(
    conn: Connection,
    settings: Settings,
    *,
    email: str,
    email_key: str,
    limit_key: str,
    ip_hash: str,
    next_path: str | None,
) -> tuple[str, int]:
    """Store a new magic link and return (token, magic_links.id). The token itself is never stored.

    Call inside the same db.transaction as ratelimit.check_link_limits. The link expires LINK_TTL after now().
    """
    token = secrets.token_urlsafe(32)
    row = conn.execute(
        "INSERT INTO magic_links (token_hash, email, email_key, limit_key, ip_hash, next_path, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, now() + %s::interval) RETURNING id",
        (digest(settings.key("magic-link"), token), email, email_key, limit_key, ip_hash, next_path, LINK_TTL),
    ).fetchone()
    if row is None:
        raise RuntimeError("insert returned no row id")
    return token, int(row["id"])


def peek_link(conn: Connection, settings: Settings, token: str) -> dict[str, Any] | None:
    """Return a live link (unused, unexpired) by token without consuming it, or None.

    For GET /auth/verify, which only shows a confirm button, so mail scanners that open links cannot spend them.
    """
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None  # cheap reject before any SQL
    return conn.execute(
        "SELECT id, email, email_key, next_path FROM magic_links"
        " WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()",
        (digest(settings.key("magic-link"), token),),
    ).fetchone()


def consume_link(conn: Connection, settings: Settings, token: str) -> dict[str, Any] | None:
    """Spend a live link and return its id, email, email_key and next_path, or None. Single use and atomic."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    return conn.execute(
        "UPDATE magic_links SET used_at = now()"
        " WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()"
        " RETURNING id, email, email_key, next_path",
        (digest(settings.key("magic-link"), token),),
    ).fetchone()


# Sessions


@dataclass(frozen=True)
class Session:
    """A session cookie whose signature, format and expiry checked out. The epoch is still the caller's to check."""

    user_id: str
    epoch: int
    expires: int
    value: str  # the raw cookie value (CSRF tokens are bound to it)


def session_cookie_name(settings: Settings) -> str:
    """The session cookie's name: with the __Host- prefix on https (Secure, no Domain, Path=/)."""
    return "__Host-sentinel_session" if settings.https else "sentinel_session"


def make_session(settings: Settings, user_id: str, epoch: int) -> str:
    """Return a signed session cookie value for this user and epoch, valid for SESSION_TTL_S from clock().

    A fresh random nonce goes into every cookie, so two sign ins never share a cookie or a CSRF token.
    """
    expires = int(clock()) + SESSION_TTL_S
    body = f"{SESSION_VERSION}.{user_id}.{int(epoch)}.{expires}.{secrets.token_urlsafe(12)}"
    return f"{body}.{_sign(settings.key('session'), body)}"


def read_session(settings: Settings, value: str | None) -> Session | None:
    """Check a session cookie's signature (constant time), format and expiry; None when any fails."""
    if not value or len(value) > MAX_SESSION:
        return None
    parts = value.split(".")
    if len(parts) != 6 or parts[0] != SESSION_VERSION:
        return None
    version, user_id, epoch, expires, nonce, signature = parts
    body = f"{version}.{user_id}.{epoch}.{expires}.{nonce}"
    if not _equal(signature, _sign(settings.key("session"), body)):
        return None
    if not user_id or not epoch.isdigit() or not expires.isdigit() or not SESSION_NONCE_RE.fullmatch(nonce):
        return None
    if int(expires) <= clock():
        return None
    return Session(user_id=user_id, epoch=int(epoch), expires=int(expires), value=value)


def cookie_kwargs(settings: Settings, max_age: int) -> dict[str, Any]:
    """Keyword arguments for Response.set_cookie: HttpOnly, SameSite=Lax, Path=/, Secure on https."""
    return {"httponly": True, "samesite": "lax", "path": "/", "secure": settings.https, "max_age": max_age}


# CSRF and origin


def csrf_token(settings: Settings, session_value: str, purpose: str) -> str:
    """A form token bound to this session cookie value and this purpose ("account" or "consent:<nonce>")."""
    return _sign(settings.key("csrf"), f"{session_value}|{purpose}")


def check_csrf(settings: Settings, session_value: str, purpose: str, given: str | None) -> bool:
    """True when the form token matches this session and purpose (constant time)."""
    if not given or not session_value:
        return False
    return _equal(given, csrf_token(settings, session_value, purpose))


def same_origin(settings: Settings, headers: Mapping[str, str]) -> bool:
    """True when a POST comes from our own pages.

    Origin must equal settings.base_url exactly; with no Origin header at all, Sec-Fetch-Site must be same-origin.
    Origin "null", a foreign origin, or neither header: False.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    origin = lowered.get("origin")
    if origin is not None:
        return origin == settings.base_url
    return lowered.get("sec-fetch-site") == "same-origin"
