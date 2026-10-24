"""The OAuth authorization server an AI app connects through: registration, consent requests, codes and tokens.

FastMCP's OAuthProvider serves /authorize, /token, /register, the discovery documents and the 401 challenge;
SentinelOAuthProvider implements its nine storage callbacks over Postgres, wraps or replaces a few of its routes and
serves its own /revoke. The flow
(plan "OAuth flow"): the app registers (DCR), sends the user to /authorize, which answers with a redirect to our
consent page carrying a signed pending request (no table) plus a short lived cookie that binds it to this browser;
the user signs in and approves (web/app.py calls approve, which writes the code); the app swaps the code for a 1 h
access token and a rotating refresh token; every MCP call then resolves its token to grant, user, agent and wallet in
load_access_token.

Storage: oauth_clients holds no secret (it is derived from the client id and recomputed in get_client); every code
and token is stored only as an HMAC under its own subkey. One oauth_codes row is one approved consent: first the
code, then the grant every token of it belongs to, so revoking that row stops them all at once. A replayed code or a
refresh token reused after the 30 second grace revokes the grant; the revocation is committed before the error is
raised. Every expiry that matters is decided by SQL now(), so tests age rows with UPDATEs.

Nothing here logs a code, token, secret, cookie or pending request; logs name client ids and user ids only.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
from functools import partial
from typing import Any, TypeVar
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.handlers.authorize import AuthorizationHandler
from fastmcp.server.auth.redirect_validation import UNSAFE_REDIRECT_URI_SCHEMES, build_client_redirect
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError, OAuthClientInformationFull, OAuthToken
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from psycopg.types.json import Jsonb
from pydantic import AnyUrl, ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp

from sentinel import db
from sentinel.web import Settings, auth, is_loopback, pages, ratelimit, users

SCOPE = "pay"
TOLERATED_SCOPES = frozenset({"pay", "offline_access"})  # offline_access is accepted, never advertised
REFRESH_SCOPES = ["pay", "offline_access"]  # what a refresh token may ask for (the SDK checks a subset); mints pay
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
CODE_TTL = "5 minutes"  # SQL interval texts
ACCESS_TTL = "1 hour"
REFRESH_TTL = "30 days"
GRANT_TTL = "90 days"
REFRESH_GRACE = "30 seconds"
CLIENT_PURGE_AGE = "30 days"  # registered clients that never completed a consent are deleted after this
PENDING_TTL_S = 20 * 60
USED_CODE_EXPIRY = 4102444800.0  # 2100-01-01: what load_authorization_code reports for a used code
MAX_PENDING = 4096
BINDING_NAME_CHARS = 16  # nonce characters in the binding cookie's name
MAX_STATE = 512
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI = 512
MAX_CLIENT_NAME = 100
MAX_METADATA = 4096
MAX_REGISTER_BODY = 16 * 1024

ACCESS_PREFIX = "sat_"
REFRESH_PREFIX = "srt_"
ACCESS_RE = re.compile(r"sat_[A-Za-z0-9_-]{43}")  # always fullmatch
REFRESH_RE = re.compile(r"srt_[A-Za-z0-9_-]{43}")
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # an S256 challenge is always 43 base64url characters
B64_RE = re.compile(r"[A-Za-z0-9_-]+")
SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*")
CSP_HOST_RE = re.compile(r"[a-z0-9.-]+|\[[0-9a-f:.]+\]")
PENDING_VERSION = 1
CONSENT_PATH = "/oauth/consent"
MCP_PATH = "/mcp"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
PRM_PATH = "/.well-known/oauth-protected-resource"
# The SDK reads client_id from the form body only, so a header only client_secret_basic client could never
# authenticate; a registration asking for it gets client_secret_post instead (RFC 7591 lets the server substitute).
AUTH_METHODS = ("none", "client_secret_post")
SUBSTITUTED_METHODS = {"client_secret_basic": "client_secret_post"}
GRANT_TYPES = ["authorization_code", "refresh_token"]
# Schemes a redirect may never use, on top of FastMCP's javascript, data, file and vbscript.
REFUSED_SCHEMES = UNSAFE_REDIRECT_URI_SCHEMES | {"about", "blob", "filesystem", "view-source", "ftp", "ws", "wss"}
# Browser schemes that wrap another URL (microsoft-edge:https://evil.example/cb), so the consent page would name the
# wrapper while the code goes elsewhere. Exact names, and prefixes for the families.
WRAPPER_SCHEMES = frozenset({"microsoft-edge", "firefox", "intent", "x-web-search", "googlechromes"})
WRAPPER_PREFIXES = ("x-safari-", "googlechrome", "opera", "microsoft-edge")
NAME_DROP = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})  # control, format, private use, surrogates, separators
# The registration fields kept; everything else a client sends is dropped (and not echoed back).
METADATA_FIELDS = (
    "redirect_uris", "client_name", "scope", "grant_types", "response_types", "token_endpoint_auth_method",
    "application_type", "software_id", "software_version",
)
AUTHORIZE_ERRORS = frozenset(
    {"invalid_request", "unauthorized_client", "access_denied", "unsupported_response_type", "invalid_scope",
     "server_error", "temporarily_unavailable", "invalid_target"}
)
DEFAULT_PORTS = {"http": 80, "https": 443}

Connect = Callable[[], AbstractContextManager[db.Connection]]
T = TypeVar("T")

clock = time.time  # pending request expiry; tests monkeypatch it
REQUEST_IP: ContextVar[str | None] = ContextVar("sentinel_request_ip", default=None)  # set by the /register wrapper
# Set by the /authorize wrapper to a list; authorize() appends the consent URL it returns, so the wrapper treats
# exactly that Location (and nothing that merely looks like it) as the consent redirect.
ISSUED_CONSENT: ContextVar[list[str] | None] = ContextVar("sentinel_issued_consent", default=None)

log = logging.getLogger(__name__)


def _hmac(key: bytes, value: str) -> bytes:
    """Raw HMAC-SHA256 of a value."""
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def _unb64(text: str) -> bytes:
    """Decode URL safe base64 without padding (the caller has checked the alphabet)."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def client_secret_for(settings: Settings, client_id: str) -> str:
    """The client secret of a confidential client: derived from its id, never stored, recomputed on every use."""
    return auth.digest(settings.key("client-secret"), client_id)


# Clients


def _redirect_parts(uri: str) -> tuple[str, str, int | None, str, str] | None:
    """(scheme, host, port, path, query) of a redirect URI for comparison, or None when it does not parse or has
    userinfo or a fragment (which this tuple could not tell apart)."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return None
    if "@" in parts.netloc or "#" in uri:
        return None
    return parts.scheme.lower(), (parts.hostname or "").lower(), port, parts.path or "/", parts.query


class SentinelClient(OAuthClientInformationFull):
    """A registered client as get_client hands it to the SDK, with Sentinel's redirect and scope rules."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        """Return the redirect URI to use for this request, or raise InvalidRedirectUriError.

        It must equal a registered URI exactly, except that http on a loopback host (exactly localhost, 127.0.0.1
        or [::1]) may use any port, with the same scheme, host, path and query (RFC 8252 7.3: native apps listen on
        a port picked at run time). Userinfo and fragments are always refused. With no redirect_uri, the one
        registered URI is used; with several registered, one must be named.
        """
        registered = list(self.redirect_uris or [])
        if redirect_uri is None:
            if len(registered) == 1:
                return registered[0]
            raise InvalidRedirectUriError("redirect_uri must be specified unless the client has exactly one")
        wanted_uri = str(redirect_uri)
        wanted = _redirect_parts(wanted_uri)
        if wanted is not None:
            loopback = wanted[0] == "http" and is_loopback(wanted[1])
            for uri in registered:
                if str(uri) == wanted_uri:
                    return redirect_uri
                have = _redirect_parts(str(uri))
                if loopback and have is not None and have[:2] == wanted[:2] and have[3:] == wanted[3:]:
                    return redirect_uri
        raise InvalidRedirectUriError("Redirect URI is not registered for this client")

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        """None stays None (the grant is pay anyway); pay and offline_access become ["pay"]; anything else raises."""
        if requested_scope is None:
            return None
        unknown = [scope for scope in requested_scope.split() if scope not in TOLERATED_SCOPES]
        if unknown:
            raise InvalidScopeError(f"Unknown scope {unknown[0][:64]}; this server only grants {SCOPE}")
        return [SCOPE]


def _has_dot_segments(path: str) -> bool:
    """True when a path has "." or ".." segments, raw or percent encoded (browsers collapse them on redirect)."""
    return any(seg in (".", "..") for candidate in (path, unquote(path)) for seg in candidate.split("/"))


def _is_wrapper_scheme(scheme: str, uri: str) -> bool:
    """True for a browser scheme that wraps another URL, or any scheme whose remainder is itself an http(s) URL."""
    if scheme in WRAPPER_SCHEMES or scheme.startswith(WRAPPER_PREFIXES):
        return True
    rest = uri.split(":", 1)[1].lstrip("/").lower() if ":" in uri else ""
    return rest.startswith(("http:", "https:"))


def _effective_port(scheme: str, port: int | None) -> int | None:
    """The port a URL really uses: the given one, else the scheme's default."""
    return port if port is not None else DEFAULT_PORTS.get(scheme)


def on_issuer_host(settings: Settings, uri: str) -> bool:
    """True when a redirect URI points at Sentinel itself: the issuer's host (on a loopback issuer, only on the
    issuer's own port, so local apps on other ports still work)."""
    try:
        parts, issuer = urlsplit(uri), urlsplit(settings.base_url)
        port, issuer_port = parts.port, issuer.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host or host != (issuer.hostname or "").lower():
        return False
    if not is_loopback(host):
        return True
    scheme = parts.scheme.lower()
    return _effective_port(scheme, port) == _effective_port(issuer.scheme, issuer_port)


def redirect_uri_problem(uri: str) -> str | None:
    """Why a redirect URI cannot be registered, in plain English, or None when it is acceptable.

    Allowed: https on a named host (or a loopback address), http only on exactly localhost, 127.0.0.1 or [::1], or
    a private use app scheme (cursor://..., com.example.app:/cb). Refused: unsafe schemes, schemes that wrap another
    URL (microsoft-edge:https://...), userinfo, fragments, dot segments, IP literals that are not loopback, the name
    localhost or any *.localhost name other than as plain http://localhost, and anything longer than
    MAX_REDIRECT_URI characters. Sentinel's own host is refused by the caller (on_issuer_host).
    """
    if len(uri) > MAX_REDIRECT_URI:
        return f"A redirect URI may have at most {MAX_REDIRECT_URI} characters."
    if "#" in uri:
        return "A redirect URI must not have a fragment."
    try:
        parts = urlsplit(uri)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        return "A redirect URI must be a valid URL."
    scheme = parts.scheme.lower()
    if not SCHEME_RE.fullmatch(scheme) or scheme in REFUSED_SCHEMES:
        return "That redirect URI scheme is not allowed."
    if _is_wrapper_scheme(scheme, uri):
        return "A redirect URI must not wrap another URL in a browser scheme."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "A redirect URI must not contain a user name or password."
    if _has_dot_segments(parts.path):
        return "A redirect URI must not contain . or .. path segments."
    host = parts.hostname
    if host:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_loopback:
            return "A redirect URI must use a host name, not an IP address (loopback aside)."
    name = (host or "").lower().rstrip(".")
    if (name == "localhost" or name.endswith(".localhost")) and not (scheme == "http" and is_loopback(host)):
        # *.localhost resolves to this machine in browsers but is never a real https host; only the plain http
        # loopback rule below may use the name.
        return "A localhost redirect URI must be plain http on exactly localhost, 127.0.0.1 or [::1]."
    if scheme == "https":
        if not host:
            return "An https redirect URI needs a host."
    elif scheme == "http":
        if not is_loopback(host):
            return "Plain http redirect URIs are only allowed on localhost, 127.0.0.1 or [::1]; use https."
    return None


def sanitize_client_name(name: str | None) -> str | None:
    """The app supplied name, safe to show: no control, format, private use or separator characters (so no bidi
    tricks), whitespace collapsed, at most MAX_CLIENT_NAME characters. None when nothing is left."""
    if not isinstance(name, str):
        return None
    kept = "".join(ch for ch in name if unicodedata.category(ch) not in NAME_DROP)
    collapsed = " ".join(kept.split())[:MAX_CLIENT_NAME].strip()
    return collapsed or None


# Pending consent requests (signed, carried in the consent URL; no table)


@dataclass(frozen=True)
class PendingRequest:
    """An /authorize request waiting for the user's consent, signed into the consent URL."""

    nonce: str  # unique per request; oauth_codes.request_nonce makes it one code per consent
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    resource: str
    expires: int  # unix seconds (clock())


_PENDING_FIELDS = {f.name for f in fields(PendingRequest)}


def _pending_mac(settings: Settings, payload: str) -> str:
    """The signature of an encoded pending request."""
    return auth.b64(_hmac(settings.key("pending-request"), payload))


def sign_pending(settings: Settings, pending: PendingRequest) -> str:
    """Encode and sign a pending request: "<b64 json>.<b64 hmac>", URL and form safe."""
    body = json.dumps({"v": PENDING_VERSION, **asdict(pending)}, separators=(",", ":"), ensure_ascii=False)
    payload = auth.b64(body.encode())
    return f"{payload}.{_pending_mac(settings, payload)}"


def _valid_pending(data: dict[str, Any]) -> bool:
    """True when decoded pending request data has exactly the fields, with the right types."""
    if set(data) != _PENDING_FIELDS:
        return False
    strings = ("nonce", "client_id", "redirect_uri", "code_challenge", "resource")
    return (
        all(isinstance(data[name], str) and data[name] for name in strings)
        and isinstance(data["redirect_uri_provided_explicitly"], bool)
        and (data["state"] is None or isinstance(data["state"], str))
        and isinstance(data["expires"], int)
        and not isinstance(data["expires"], bool)
    )


def read_pending(settings: Settings, req: str | None) -> PendingRequest | None:
    """Check and decode a signed pending request; None when missing, too long, tampered, malformed or expired."""
    if not isinstance(req, str) or not req or len(req) > MAX_PENDING:
        return None
    payload, dot, signature = req.partition(".")
    if not dot or not B64_RE.fullmatch(payload) or not B64_RE.fullmatch(signature):
        return None
    if not hmac.compare_digest(signature.encode(), _pending_mac(settings, payload).encode()):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, binascii.Error):  # bad base64, bad UTF-8 and bad JSON are all ValueErrors
        return None
    if not isinstance(data, dict) or data.pop("v", None) != PENDING_VERSION or not _valid_pending(data):
        return None
    pending = PendingRequest(**data)
    if pending.expires <= clock():
        return None
    return pending


def binding_value(settings: Settings, nonce: str) -> str:
    """The consent binding cookie's value for one request: only the browser that started the flow carries it."""
    return auth.b64(_hmac(settings.key("consent-binding"), nonce))


def binding_cookie_name(settings: Settings, nonce: str) -> str:
    """The consent binding cookie's name for one request: "sentinel_consent_" plus the first BINDING_NAME_CHARS
    characters of its nonce (one cookie per flow, so a second /authorize in the same browser does not overwrite the
    first), with the __Host- prefix on https."""
    suffix = re.sub(r"[^A-Za-z0-9_-]", "", nonce)[:BINDING_NAME_CHARS]  # nonces are token_urlsafe; keep it a token
    return f"{'__Host-' if settings.https else ''}sentinel_consent_{suffix}"


# Redirect targets, as the consent page shows them


def is_trusted_redirect(uri: str) -> bool:
    """True for exactly a Claude callback, or http on exactly localhost, 127.0.0.1 or [::1]: the only targets an
    /authorize error may be redirected to without the user seeing it first (anything else gets an error page, so no
    open redirector)."""
    if uri in CLAUDE_CALLBACKS:
        return True
    try:
        parts = urlsplit(uri)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        return False
    no_userinfo = parts.username is None and parts.password is None and "@" not in parts.netloc
    return parts.scheme == "http" and no_userinfo and is_loopback(parts.hostname)


def _ascii_host(host: str) -> str:
    """A host in its ASCII (punycode) form, so look alike Unicode names show what they really are."""
    if ":" in host:
        return f"[{host}]"  # IPv6 literal
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.encode("ascii", "backslashreplace").decode("ascii").lower()


def redirect_display(uri: str) -> str:
    """How the consent page names where the code goes: host[:port] (punycode) for http(s); for any other scheme
    the whole URI, since an app scheme's host says little about which app receives it."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return "an unreadable address"
    if parts.scheme not in DEFAULT_PORTS:
        return uri
    return _ascii_host(parts.hostname or "") + (f":{port}" if port is not None else "")


def form_action_source(uri: str) -> str:
    """The CSP form-action source the consent response adds so the browser follows the redirect after Allow:
    "https://claude.ai", "http://localhost:6274" or "cursor:". "'none'" for anything that would not be a clean
    source (which adds nothing)."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return "'none'"
    scheme = parts.scheme.lower()
    if not SCHEME_RE.fullmatch(scheme):
        return "'none'"
    if scheme not in DEFAULT_PORTS:
        return f"{scheme}:"
    host = _ascii_host(parts.hostname or "")
    if not host or not CSP_HOST_RE.fullmatch(host):
        return "'none'"
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def _resource_parts(url: str) -> tuple[str, str, int, str] | None:
    """(scheme, host, port, path without one trailing slash) of a resource URL, or None when it is not one."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or "?" in url or "#" in url or "@" in parts.netloc or not parts.hostname:
        return None
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return scheme, parts.hostname.lower(), port or DEFAULT_PORTS[scheme], path


def canonical_resource(settings: Settings, resource: str | None) -> str | None:
    """The resource a grant is for: always settings.resource_url, or None when the request names another server.

    Absent means ours (one Claude Code version omits it); our URL with one trailing slash or another host case is
    ours too. Anything else is None, which the caller answers with invalid_target.
    """
    if resource is None:
        return settings.resource_url
    if not isinstance(resource, str) or len(resource) > MAX_REDIRECT_URI:
        return None
    got = _resource_parts(resource)
    if got is None or got != _resource_parts(settings.resource_url):
        return None
    return settings.resource_url


# Consent outcomes (called by web/app.py)


def approve(
    conn: db.Connection, settings: Settings, pending: PendingRequest, user_id: str, *, session_epoch: int
) -> str | None:
    """Record the user's consent and return the redirect that carries the code back to the app.

    Inserts the oauth_codes row (the code's hash, the request nonce, client, user, redirect, PKCE challenge, scope
    pay, resource; the code expires in CODE_TTL and the grant in GRANT_TTL) and returns redirect_uri with code,
    state (only when the app sent one) and iss. Returns None when this nonce already has a code (a second Allow):
    the caller shows "already approved". Raises ValueError when the user has no wallet yet, so no code is ever
    issued without one, or when the request is for another resource. Raises PermissionError("signed out") when the
    user's session_epoch is no longer the one the Allow was made under: the user row is read FOR SHARE in the same
    transaction, so a concurrent "sign out everywhere" either runs first (and this refuses) or waits for the insert
    (and then revokes the new grant too).
    """
    if users.identity_for(conn, user_id) is None:
        raise ValueError("The user has no wallet yet; no code is issued without one.")
    if pending.resource != settings.resource_url:
        raise ValueError("The request is for another resource.")
    code = secrets.token_urlsafe(32)
    try:
        with db.transaction(conn):
            current = conn.execute(
                "SELECT id FROM users WHERE id = %s AND session_epoch = %s FOR SHARE",
                (users.canonical_id(user_id), session_epoch),
            ).fetchone()
            if current is None:
                raise PermissionError("signed out")
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, request_nonce, client_id, user_id, redirect_uri,"
                " redirect_uri_provided_explicitly, code_challenge, scopes, resource, expires_at, grant_expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s::interval, now() + %s::interval)",
                (
                    auth.digest(settings.key("auth-code"), code), pending.nonce, pending.client_id,
                    users.canonical_id(user_id), pending.redirect_uri, pending.redirect_uri_provided_explicitly,
                    pending.code_challenge, [SCOPE], pending.resource, CODE_TTL, GRANT_TTL,
                ),
            )
            conn.execute("UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s", (pending.client_id,))
    except db.UniqueViolation:
        return None
    log.info("consent approved: user %s, client %s", user_id, pending.client_id)
    params = {"code": code}
    if pending.state is not None:
        params["state"] = pending.state
    return build_client_redirect(pending.redirect_uri, params, iss=settings.base_url)


def deny_redirect(settings: Settings, pending: PendingRequest) -> str:
    """The redirect that tells the app the user said no: error=access_denied, state (if sent) and iss."""
    params = {"error": "access_denied", "error_description": "The user denied the request."}
    if pending.state is not None:
        params["state"] = pending.state
    return build_client_redirect(pending.redirect_uri, params, iss=settings.base_url)


# Tokens


def _token_hash(settings: Settings, token: str) -> str | None:
    """The stored hash of an access or refresh token, by its prefix; None for any other shape."""
    if ACCESS_RE.fullmatch(token):
        return auth.digest(settings.key("access-token"), token)
    if REFRESH_RE.fullmatch(token):
        return auth.digest(settings.key("refresh-token"), token)
    return None


def _store_pair(conn: db.Connection, settings: Settings, grant_id: int, access: str, refresh: str) -> OAuthToken:
    """Insert an access and refresh token for a grant (skipping ones already there) and return the token response.

    Access tokens live ACCESS_TTL and refresh tokens REFRESH_TTL, both capped by the grant's own expiry.
    ON CONFLICT DO NOTHING makes a retried rotation return the same pair instead of forking the chain.
    """
    access_hash = auth.digest(settings.key("access-token"), access)
    for token_hash, kind, ttl in (
        (access_hash, "access", ACCESS_TTL),
        (auth.digest(settings.key("refresh-token"), refresh), "refresh", REFRESH_TTL),
    ):
        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, grant_id, expires_at)"
            " SELECT %s, %s, id, least(now() + %s::interval, grant_expires_at) FROM oauth_codes WHERE id = %s"
            " ON CONFLICT (token_hash) DO NOTHING",
            (token_hash, kind, ttl, grant_id),
        )
    row = conn.execute(
        "SELECT ceil(extract(epoch FROM expires_at - now()))::int AS expires_in FROM oauth_tokens"
        " WHERE token_hash = %s AND grant_id = %s",
        (access_hash, grant_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"grant {grant_id} has no access token after minting")
    return OAuthToken(
        access_token=access, token_type="Bearer", expires_in=max(0, int(row["expires_in"])), scope=SCOPE,
        refresh_token=refresh,
    )


def issue_tokens(conn: db.Connection, settings: Settings, grant_id: int) -> OAuthToken:
    """Mint a fresh random access and refresh token pair for a grant (a code exchange) and store their hashes."""
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    return _store_pair(conn, settings, grant_id, access, refresh)


def rotated_pair(settings: Settings, refresh_token: str) -> tuple[str, str]:
    """The (access, refresh) pair a refresh token rotates into: derived from it, so every retry gets the same."""
    key = settings.key("refresh-rotation")
    return (
        ACCESS_PREFIX + auth.b64(_hmac(key, "access|" + refresh_token)),
        REFRESH_PREFIX + auth.b64(_hmac(key, "refresh|" + refresh_token)),
    )


def _grant_usable(conn: db.Connection, settings: Settings, user_id: str, resource: str) -> bool:
    """True when a grant may still mint tokens: it is for our resource and its user has a wallet and books."""
    return resource == settings.resource_url and users.identity_for(conn, user_id) is not None


def _tokens_or_raise(result: OAuthToken | str) -> OAuthToken:
    """Return the token response, or raise invalid_grant with the reason a worker thread returned.

    Raised here, on the event loop, because the SDK's TokenError is a frozen dataclass and cannot carry a
    traceback back out of asyncio.to_thread.
    """
    if isinstance(result, str):
        raise TokenError("invalid_grant", result)
    return result


def _successor_used(conn: db.Connection, settings: Settings, refresh: str) -> bool:
    """True when the refresh token a rotation derives has already been used itself."""
    row = conn.execute(
        "SELECT 1 FROM oauth_tokens WHERE token_hash = %s AND kind = 'refresh' AND used_at IS NOT NULL",
        (auth.digest(settings.key("refresh-token"), refresh),),
    ).fetchone()
    return row is not None


def _revoke(conn: db.Connection, grant_id: int) -> None:
    """Revoke a grant (idempotent): every token of it stops at once."""
    conn.execute("UPDATE oauth_codes SET revoked_at = coalesce(revoked_at, now()) WHERE id = %s", (grant_id,))


class SentinelOAuthProvider(OAuthProvider):
    """FastMCP's OAuth authorization server with Sentinel's storage, rules and routes. See the module docstring."""

    def __init__(self, settings: Settings, connect: Connect = db.connection) -> None:
        """Configure FastMCP for one issuer (settings.base_url), DCR, revocation and the one scope, pay."""
        super().__init__(
            base_url=settings.base_url,
            issuer_url=settings.base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["pay", "offline_access"], default_scopes=["pay"]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        )
        self.settings = settings
        self._connect = connect

    @property
    def scopes_supported(self) -> list[str]:
        """Advertise only pay (offline_access is tolerated, never advertised)."""
        return [SCOPE]

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run fn(conn, *args) on a connection of its own in a worker thread."""

        def work() -> T:
            with self._connect() as conn:
                return fn(conn, *args)

        return await asyncio.to_thread(work)

    # Clients

    async def get_client(self, client_id: str) -> SentinelClient | None:
        """The registered client with its derived secret, or None for an unknown or malformed id."""
        if not isinstance(client_id, str) or users.canonical_id(client_id) is None:
            return None  # DCR ids are uuids; anything else is not ours, and never reaches SQL
        row = await self._run(_client_row, client_id)
        if row is None:
            return None
        method = SUBSTITUTED_METHODS.get(row["token_endpoint_auth_method"], row["token_endpoint_auth_method"])
        secret = client_secret_for(self.settings, client_id) if method != "none" else None
        try:
            return SentinelClient.model_validate(
                {
                    **row["metadata"],
                    "client_id": client_id,
                    "client_secret": secret,
                    "client_secret_expires_at": 0 if secret else None,
                    "client_id_issued_at": int(row["created_at"].timestamp()),
                    "token_endpoint_auth_method": method,
                }
            )
        except ValidationError:
            log.warning("stored metadata of client %s does not load", client_id)
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Check a new registration against Sentinel's rules, normalize it and store it.

        Runs after the SDK's own checks. Mutates client_info, which the SDK then echoes: the derived secret, the
        sanitized name, scope pay, both grant types, client_secret_post in place of client_secret_basic, and every
        field outside the allowlist cleared. Raises RegistrationError for a rule broken, ratelimit.RateLimited
        (answered 429 by the /register wrapper) when the IP or the overall budget registered too many. Never used
        clients older than 30 days are purged first.
        """
        requested = client_info.token_endpoint_auth_method or "client_secret_post"
        method = SUBSTITUTED_METHODS.get(requested, requested)
        metadata = _checked_metadata(self.settings, client_info, method)
        client_info.client_secret = (
            client_secret_for(self.settings, client_info.client_id) if method != "none" else None
        )
        client_info.client_secret_expires_at = 0 if client_info.client_secret else None
        ip = REQUEST_IP.get() or ""
        await self._run(
            _store_client, client_info.client_id, metadata, method,
            ratelimit.registration_ip_hash(self.settings, ip), ratelimit.is_exempt(ip),
        )
        log.info("registered client %s (%s)", client_info.client_id, method)

    # Authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Turn a checked /authorize request into our consent page URL with the signed pending request.

        The SDK has checked the client, redirect URI, scope and PKCE method. Here: the resource must be ours
        (absent counts as ours), state at most MAX_STATE characters, an S256 challenge of the right shape, and the
        signed request at most MAX_PENDING characters.
        """
        if on_issuer_host(self.settings, str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "The redirect URI must not point at this server")
        resource = canonical_resource(self.settings, params.resource)
        if resource is None:
            raise AuthorizeError("invalid_target", f"This server only issues tokens for {self.settings.resource_url}")
        if params.state is not None and len(params.state) > MAX_STATE:
            raise AuthorizeError("invalid_request", f"state may have at most {MAX_STATE} characters")
        if not CHALLENGE_RE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 challenge (43 characters)")
        pending = PendingRequest(
            nonce=secrets.token_urlsafe(24),
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            state=params.state,
            resource=resource,
            expires=int(clock()) + PENDING_TTL_S,
        )
        req = sign_pending(self.settings, pending)
        if len(req) > MAX_PENDING:
            raise AuthorizeError("invalid_request", "The authorization request is too large")
        log.info("authorization request from client %s", client.client_id)
        url = f"{self.settings.base_url}{CONSENT_PATH}?req={req}"
        issued = ISSUED_CONSENT.get()
        if issued is not None:
            issued.append(url)
        return url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """The code issued to this client, or None. Used codes are returned too, with a far future expires_at so the
        SDK's expiry check does not stop a late replay: it reaches exchange_authorization_code (after the SDK's PKCE
        and redirect checks), which revokes the grant. Unused expired codes keep their real expiry and are refused."""
        if not isinstance(authorization_code, str) or not auth.TOKEN_RE.fullmatch(authorization_code):
            return None
        row = await self._run(
            _code_row, auth.digest(self.settings.key("auth-code"), authorization_code), client.client_id
        )
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(row["scopes"]),
            expires_at=USED_CODE_EXPIRY if row["used_at"] is not None else row["expires_at"].timestamp(),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Spend the code atomically and mint the first token pair; a replay revokes the grant (invalid_grant)."""
        return _tokens_or_raise(await self._run(self._exchange_code, client.client_id, authorization_code.code))

    def _exchange_code(self, conn: db.Connection, client_id: str, code: str) -> OAuthToken | str:
        """The code exchange on one connection: the token pair, or why it failed. A replay's revocation is
        committed here, before the caller raises."""
        code_hash = auth.digest(self.settings.key("auth-code"), code)
        failure = "authorization code is not valid"
        with db.transaction(conn):
            row = conn.execute(
                "UPDATE oauth_codes SET used_at = now() WHERE code_hash = %s AND client_id = %s AND used_at IS NULL"
                " AND revoked_at IS NULL AND expires_at > now() AND grant_expires_at > now()"
                " RETURNING id, user_id::text AS user_id, resource",
                (code_hash, client_id),
            ).fetchone()
            if row is None:
                seen = conn.execute(
                    "SELECT id, used_at FROM oauth_codes WHERE code_hash = %s AND client_id = %s",
                    (code_hash, client_id),
                ).fetchone()
                if seen is not None and seen["used_at"] is not None:
                    _revoke(conn, seen["id"])
                    log.warning("authorization code replayed: client %s, grant %s revoked", client_id, seen["id"])
                    failure = "authorization code was already used; the grant is revoked"
                elif seen is not None:
                    failure = "authorization code has expired"
            elif _grant_usable(conn, self.settings, row["user_id"], row["resource"]):
                conn.execute("UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s", (client_id,))
                tokens = issue_tokens(conn, self.settings, row["id"])
                log.info("code exchanged: client %s, user %s, grant %s", client_id, row["user_id"], row["id"])
                return tokens
        return failure

    # Refresh

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        """The refresh token issued to this client under a live grant, or None. Used tokens are returned too, so
        exchange_refresh_token can tell a retry within the grace from a reuse."""
        if not isinstance(refresh_token, str) or not REFRESH_RE.fullmatch(refresh_token):
            return None
        row = await self._run(
            _refresh_row, auth.digest(self.settings.key("refresh-token"), refresh_token), client.client_id
        )
        if row is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=list(REFRESH_SCOPES),  # so "pay offline_access" passes the SDK's subset check; pay is minted
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate a refresh token into a new pair; a reuse after REFRESH_GRACE revokes the grant (invalid_grant)."""
        return _tokens_or_raise(await self._run(self._rotate, client.client_id, refresh_token.token))

    def _rotate(self, conn: db.Connection, client_id: str, presented: str) -> OAuthToken | str:
        """The rotation on one connection, with the grant row locked so concurrent refreshes queue up: the new
        pair, or why it failed (a reuse's revocation is committed here, before the caller raises).

        The presented token is marked used with coalesce(used_at, now()), so its first use time sticks; within
        the grace the derived pair is inserted ON CONFLICT DO NOTHING and returned (the same pair every time), but
        only while its refresh token is itself unused: once the successor has been rotated, presenting the old one
        is a reuse and revokes the grant.
        """
        token_hash = auth.digest(self.settings.key("refresh-token"), presented)
        failure = "refresh token is not valid"
        with db.transaction(conn):
            row = conn.execute(
                "SELECT t.id AS token_id, g.id AS grant_id, g.user_id::text AS user_id, g.resource,"
                " (g.revoked_at IS NULL AND g.grant_expires_at > now()) AS live_grant,"
                " t.expires_at > now() AS live_token"
                " FROM oauth_tokens t JOIN oauth_codes g ON g.id = t.grant_id"
                " WHERE t.token_hash = %s AND t.kind = 'refresh' AND g.client_id = %s FOR UPDATE OF g",
                (token_hash, client_id),
            ).fetchone()
            if row is not None and row["live_grant"] and row["live_token"]:
                mark = conn.execute(
                    "UPDATE oauth_tokens SET used_at = coalesce(used_at, now()) WHERE id = %s"
                    " RETURNING used_at > now() - %s::interval AS in_grace",
                    (row["token_id"], REFRESH_GRACE),
                ).fetchone()
                access, refresh = rotated_pair(self.settings, presented)
                if mark is None or not mark["in_grace"] or _successor_used(conn, self.settings, refresh):
                    _revoke(conn, row["grant_id"])
                    log.warning("refresh token reused: client %s, grant %s revoked", client_id, row["grant_id"])
                    failure = "refresh token was already used; the grant is revoked"
                elif _grant_usable(conn, self.settings, row["user_id"], row["resource"]):
                    return _store_pair(conn, self.settings, row["grant_id"], access, refresh)
            elif row is not None:
                failure = "refresh token or grant has expired or was revoked"
        return failure

    # Access

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Resolve a bearer token to who it acts for, or None. Never raises.

        Requires a live access token under a grant that is neither revoked nor past its 90 days, the resource
        B/mcp (FastMCP does not check audience), and a user with a wallet whose agent has its own cash account.
        Returns subject=user_id and claims iss, sub, agent and wallet.
        """
        try:
            if not isinstance(token, str) or not ACCESS_RE.fullmatch(token):
                return None
            return await self._run(self._access, token)
        except Exception as exc:  # a database error must read as "not authenticated", never as a crash
            log.warning("access token check failed: %s", type(exc).__name__)
            return None

    def _access(self, conn: db.Connection, token: str) -> AccessToken | None:
        """The access token lookup and identity join on one connection."""
        row = conn.execute(
            "SELECT t.expires_at, g.client_id, g.scopes, g.resource, g.user_id::text AS user_id"
            " FROM oauth_tokens t JOIN oauth_codes g ON g.id = t.grant_id"
            " WHERE t.token_hash = %s AND t.kind = 'access' AND t.expires_at > now()"
            " AND g.revoked_at IS NULL AND g.grant_expires_at > now()",
            (auth.digest(self.settings.key("access-token"), token),),
        ).fetchone()
        if row is None or row["resource"] != self.settings.resource_url:
            return None
        identity = users.identity_for(conn, row["user_id"])
        if identity is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=list(row["scopes"]),
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            subject=identity.user_id,
            claims=identity.claims(self.settings.base_url),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke the whole grant the token belongs to, so its access and refresh tokens all stop."""
        token_hash = _token_hash(self.settings, token.token) if isinstance(token.token, str) else None
        if token_hash is None:
            return
        grant = await self._run(_revoke_by_token, token_hash)
        if grant is not None:
            log.info("grant %s revoked by client %s", grant, token.client_id)

    # Routes

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """FastMCP's routes with Sentinel's metadata, PRM at both paths, the /authorize and /register wrappers and
        Sentinel's own /revoke (the SDK's requires a client_secret field, so public clients could not revoke).

        /token stays as FastMCP builds it. MCP must be mounted at /mcp: every token is bound to
        settings.resource_url.
        """
        if mcp_path not in (None, MCP_PATH):
            raise ValueError(f"Sentinel serves MCP at {MCP_PATH} only (tokens are bound to that resource)")
        authorize_handler = AuthorizationHandler(
            provider=self, base_url=self.settings.base_url, issuer_url=self.settings.base_url
        )
        registration_handler = RegistrationHandler(
            self, options=self.client_registration_options or ClientRegistrationOptions()
        )

        client_authenticator = ClientAuthenticator(self)

        async def authorize_endpoint(request: Request) -> Response:
            """/authorize: FastMCP's handler, then the binding cookie or the no open redirect rule."""
            issued: list[str] = []
            reset = ISSUED_CONSENT.set(issued)
            try:
                response = await authorize_handler.handle(request)
            finally:
                ISSUED_CONSENT.reset(reset)
            return self._finish_authorize(request, response, issued[-1] if issued else None)

        async def register_endpoint(request: Request) -> Response:
            """/register: JSON only (415 otherwise), then the SDK's handler with the client IP known to
            register_client, and 429 when limited."""
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return JSONResponse(
                    {"error": "invalid_client_metadata", "error_description": "Send the registration as JSON."},
                    status_code=415,
                    headers={"Cache-Control": "no-store"},
                )
            reset = REQUEST_IP.set(request.client.host if request.client else None)
            try:
                return await registration_handler.handle(request)
            except ratelimit.RateLimited as exc:
                return JSONResponse(
                    {"error": "rate_limited", "error_description": "Too many registrations; try again later."},
                    status_code=429,
                    headers={"Retry-After": str(exc.retry_after), "Cache-Control": "no-store"},
                )
            finally:
                REQUEST_IP.reset(reset)

        async def revoke_endpoint(request: Request) -> Response:
            """/revoke (RFC 7009): authenticate the client the SDK's way, then revoke the token's grant only when the
            token belongs to that client. Always 200 for an authenticated client, known token or not."""
            no_store = {"Cache-Control": "no-store", "Pragma": "no-cache"}
            try:
                client = await client_authenticator.authenticate_request(request)
            except AuthenticationError as exc:
                return JSONResponse(
                    {"error": "invalid_client", "error_description": exc.message}, status_code=401, headers=no_store
                )
            form = await request.form()
            token, hint = form.get("token"), form.get("token_type_hint")
            if not isinstance(token, str) or not token:
                return JSONResponse(
                    {"error": "invalid_request", "error_description": "token is required"},
                    status_code=400,
                    headers=no_store,
                )
            loaders: list[Callable[[str], Awaitable[AccessToken | RefreshToken | None]]] = [
                self.load_access_token, partial(self.load_refresh_token, client)
            ]
            if hint == "refresh_token":
                loaders.reverse()
            for loader in loaders:
                found = await loader(token)
                if found is not None:
                    if found.client_id == client.client_id:
                        await self.revoke_token(found)
                    break
            return Response(status_code=200, headers=no_store)

        async def as_metadata(request: Request) -> Response:
            """RFC 8414 metadata, built from plain strings so no URL gains a trailing slash."""
            return JSONResponse(self.metadata(), headers={"Cache-Control": "public, max-age=3600"})

        async def resource_metadata(request: Request) -> Response:
            """RFC 9728 protected resource metadata for B/mcp."""
            return JSONResponse(self.resource_metadata(), headers={"Cache-Control": "public, max-age=3600"})

        prm_mcp = PRM_PATH + MCP_PATH
        ours: dict[str, Route] = {
            AS_METADATA_PATH: Route(
                AS_METADATA_PATH, endpoint=cors_middleware(as_metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"]
            ),
            prm_mcp: Route(
                prm_mcp, endpoint=cors_middleware(resource_metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"]
            ),
            "/authorize": Route(
                "/authorize",
                endpoint=RequestBodyLimitMiddleware(request_response(authorize_endpoint), MAX_REGISTER_BODY),
                methods=["GET", "POST"],
            ),
            "/register": Route(
                "/register",
                endpoint=_cors(
                    RequestBodyLimitMiddleware(request_response(register_endpoint), MAX_REGISTER_BODY),
                    ["POST", "OPTIONS"],
                ),
                methods=["POST", "OPTIONS"],
            ),
            "/revoke": Route(
                "/revoke",
                endpoint=_cors(
                    RequestBodyLimitMiddleware(request_response(revoke_endpoint), MAX_REGISTER_BODY),
                    ["POST", "OPTIONS"],
                ),
                methods=["POST", "OPTIONS"],
            ),
        }
        routes: list[Route] = []
        for route in super().get_routes(MCP_PATH):
            if route.path in ours:
                routes.append(ours.pop(route.path))
            elif route.path != PRM_PATH:
                routes.append(route)
        routes.extend(ours.values())  # any the SDK did not build
        routes.append(
            Route(PRM_PATH, endpoint=cors_middleware(resource_metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])
        )
        return routes

    def metadata(self) -> dict[str, Any]:
        """The authorization server metadata document (RFC 8414), issuer exactly settings.base_url."""
        base = self.settings.base_url
        return {
            "issuer": base,
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register",
            "revocation_endpoint": base + "/revoke",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": list(GRANT_TYPES),
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        }

    def resource_metadata(self) -> dict[str, Any]:
        """The protected resource metadata document (RFC 9728) for B/mcp."""
        return {
            "resource": self.settings.resource_url,
            "authorization_servers": [self.settings.base_url],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Sentinel",
        }

    def _finish_authorize(self, request: Request, response: Response, issued: str | None) -> Response:
        """Bind a consent redirect to this browser, pass an error redirect only to a trusted target, and turn every
        other outcome into the site's error page (status 400, no Location), so /authorize is never an open
        redirector. issued is the consent URL authorize() returned for this request (None when it returned none);
        only a Location equal to it gets the binding cookie."""
        location = response.headers.get("location")
        if 300 <= response.status_code < 400 and location:
            if issued is not None and location == issued:
                req = parse_qs(urlsplit(location).query).get("req", [None])[0]
                pending = read_pending(self.settings, req)
                if pending is None:  # cannot happen for a URL authorize() just built; refuse rather than guess
                    return _authorize_error(request, "server_error")
                response.set_cookie(
                    binding_cookie_name(self.settings, pending.nonce), binding_value(self.settings, pending.nonce),
                    **auth.cookie_kwargs(self.settings, PENDING_TTL_S),
                )
                return response
            parts = urlsplit(location)
            error = (parse_qs(parts.query).get("error") or ["invalid_request"])[0]
            if is_trusted_redirect(urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))):
                return response
            log.info("authorize error for an untrusted redirect shown as a page instead")
            return _authorize_error(request, error)
        if response.status_code >= 400:
            error = "invalid_request"
            try:
                body = json.loads(bytes(response.body))
                error = body.get("error", error) if isinstance(body, dict) else error
            except (ValueError, AttributeError):
                pass  # FastMCP's own HTML page for an unknown client
            return _authorize_error(request, error, 500 if response.status_code >= 500 else 400)
        return response


def _authorize_error(request: Request, error: str, status: int = 400) -> Response:
    """The site error page for a connection request that cannot continue. Only a known error code is shown."""
    code = error if error in AUTHORIZE_ERRORS else "invalid_request"
    return pages.render_error(
        request,
        "This connection request cannot continue",
        f"The app that sent you here made a request Sentinel cannot accept ({code}). "
        "Go back to the app and connect again.",
        status,
    )


def _cors(app: ASGIApp, methods: list[str]) -> ASGIApp:
    """CORS for an endpoint browser based clients call directly, outermost so even a 413 carries it."""
    return CORSMiddleware(
        app=app, allow_origins="*", allow_methods=methods, allow_headers=[MCP_PROTOCOL_VERSION_HEADER]
    )


def _checked_metadata(settings: Settings, client_info: OAuthClientInformationFull, method: str) -> dict[str, Any]:
    """Apply the registration rules to client_info (mutating it as the SDK will echo it) and return the
    allowlisted metadata to store. Raises RegistrationError naming the first rule broken."""
    uris = [str(uri) for uri in client_info.redirect_uris or []]
    if not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise RegistrationError("invalid_redirect_uri", f"Register between 1 and {MAX_REDIRECT_URIS} redirect URIs.")
    for uri in uris:
        problem = redirect_uri_problem(uri)
        if problem is None and on_issuer_host(settings, uri):
            problem = "A redirect URI must not point at this server."
        if problem is not None:
            raise RegistrationError("invalid_redirect_uri", problem)
    if client_info.jwks is not None or client_info.jwks_uri is not None:
        raise RegistrationError("invalid_client_metadata", "jwks and jwks_uri are not supported.")
    if method not in AUTH_METHODS:
        raise RegistrationError("invalid_client_metadata", f"token_endpoint_auth_method must be one of {AUTH_METHODS}")
    if not set((client_info.scope or SCOPE).split()) <= TOLERATED_SCOPES:
        raise RegistrationError("invalid_client_metadata", f"The only scope is {SCOPE}.")
    client_info.token_endpoint_auth_method = method
    client_info.scope = SCOPE
    client_info.client_name = sanitize_client_name(client_info.client_name)
    client_info.grant_types = list(GRANT_TYPES)
    client_info.response_types = ["code"]
    client_info.client_uri = client_info.logo_uri = client_info.tos_uri = client_info.policy_uri = None
    client_info.contacts = None
    metadata: dict[str, Any] = {}
    for name in METADATA_FIELDS:
        value = uris if name == "redirect_uris" else getattr(client_info, name)
        if value is not None:
            metadata[name] = value
    if len(json.dumps(metadata, ensure_ascii=False).encode()) > MAX_METADATA:
        raise RegistrationError("invalid_client_metadata", f"Client metadata may be at most {MAX_METADATA} bytes.")
    return metadata


# SQL that runs in worker threads (each takes the connection first)


def _client_row(conn: db.Connection, client_id: str) -> dict[str, Any] | None:
    """The stored registration of a client."""
    return conn.execute(
        "SELECT metadata, token_endpoint_auth_method, created_at FROM oauth_clients WHERE client_id = %s",
        (client_id,),
    ).fetchone()


def _store_client(
    conn: db.Connection, client_id: str, metadata: dict[str, Any], method: str, ip_hash: str, exempt: bool
) -> None:
    """Purge stale clients, check the registration limits and insert the client, in one transaction."""
    with db.transaction(conn):
        conn.execute(
            "DELETE FROM oauth_clients WHERE last_used_at IS NULL AND created_at < now() - %s::interval",
            (CLIENT_PURGE_AGE,),
        )
        ratelimit.check_registration_limits(conn, ip_hash=ip_hash, exempt=exempt)
        conn.execute(
            "INSERT INTO oauth_clients (client_id, metadata, token_endpoint_auth_method, created_ip_hash)"
            " VALUES (%s, %s, %s, %s)",
            (client_id, Jsonb(metadata), method, ratelimit.stored_registration_hash(ip_hash, exempt)),
        )


def _code_row(conn: db.Connection, code_hash: str, client_id: str) -> dict[str, Any] | None:
    """A code of this client under a grant not revoked, used or not."""
    return conn.execute(
        "SELECT client_id, user_id::text AS user_id, redirect_uri, redirect_uri_provided_explicitly,"
        " code_challenge, scopes, resource, expires_at, used_at FROM oauth_codes"
        " WHERE code_hash = %s AND client_id = %s AND revoked_at IS NULL",
        (code_hash, client_id),
    ).fetchone()


def _refresh_row(conn: db.Connection, token_hash: str, client_id: str) -> dict[str, Any] | None:
    """A refresh token of this client under a grant not revoked, used or not."""
    return conn.execute(
        "SELECT t.expires_at, g.client_id, g.scopes, g.resource, g.user_id::text AS user_id"
        " FROM oauth_tokens t JOIN oauth_codes g ON g.id = t.grant_id"
        " WHERE t.token_hash = %s AND t.kind = 'refresh' AND g.client_id = %s AND g.revoked_at IS NULL",
        (token_hash, client_id),
    ).fetchone()


def _revoke_by_token(conn: db.Connection, token_hash: str) -> int | None:
    """Revoke the grant a token belongs to; its id, or None for an unknown token."""
    row = conn.execute(
        "UPDATE oauth_codes SET revoked_at = coalesce(revoked_at, now())"
        " WHERE id = (SELECT grant_id FROM oauth_tokens WHERE token_hash = %s) RETURNING id",
        (token_hash,),
    ).fetchone()
    return int(row["id"]) if row else None

