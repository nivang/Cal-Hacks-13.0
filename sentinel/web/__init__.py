"""Sentinel web: the sign in site and the OAuth server for MCP, and the settings both run on.

This module reads and checks the environment once, at startup (mcp_server.main() calls missing_env, then
load_settings), and hands every other web module a frozen Settings. Settings.base_url is PUBLIC_BASE_URL normalized
to scheme://host[:port] with no trailing slash; that exact string is the OAuth issuer, the origin the site's CSRF
check compares against, and the prefix of the MCP resource URL.

Dev mode means PUBLIC_BASE_URL and the bind host are both loopback. Only then may the mail settings be empty (sign in
links print to stderr) and SENTINEL_ALLOW_LOCAL_URLS be set. Two secrets feed every HMAC: SESSION_SECRET signs what
the browser carries (session cookie, CSRF tokens, the pending consent request and its binding cookie) and
TOKEN_HASH_SECRET hashes what the database stores (links, codes, tokens, client secrets, IPs). key(purpose) derives
one subkey per purpose, so a value made for one use can never pass as another.
"""
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SESSION_PURPOSES = frozenset({"session", "csrf", "pending-request", "consent-binding"})
TOKEN_PURPOSES = frozenset(
    {"magic-link", "auth-code", "access-token", "refresh-token", "refresh-rotation", "client-secret", "ip"}
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})  # matches mcp's validate_issuer_url

MIN_SECRET_LENGTH = 32
REQUIRED_ENV = ("DATABASE_URL", "PUBLIC_BASE_URL", "SESSION_SECRET", "TOKEN_HASH_SECRET", "ANTHROPIC_API_KEY")
MAIL_ENV = ("RESEND_API_KEY", "MAIL_FROM")
DEFAULT_PORTS = {"http": 80, "https": 443}


class ConfigError(Exception):
    """The environment cannot run the server. problems lists every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        """Keep the full list and join it into the message."""
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    """Everything the web modules need from the environment, checked and normalized by load_settings."""

    base_url: str  # scheme://host[:port], lowercase host, no path or trailing slash: the OAuth issuer everywhere
    session_secret: bytes = field(repr=False)  # secrets stay out of repr, so logs and test diffs never show them
    token_hash_secret: bytes = field(repr=False)
    resend_api_key: str | None = field(repr=False)  # None in dev mode means: print links to stderr
    mail_from: str | None
    bind_host: str
    dev_mode: bool
    warnings: tuple[str, ...] = ()  # e.g. FORWARDED_ALLOW_IPS is "*" or unset

    @property
    def resource_url(self) -> str:
        """The MCP endpoint, which is also the OAuth resource every token is bound to."""
        return self.base_url + "/mcp"

    @property
    def https(self) -> bool:
        """True when the site is served over https (Secure and __Host- cookies, HSTS)."""
        return self.base_url.startswith("https://")

    def key(self, purpose: str) -> bytes:
        """Return the subkey for one purpose: HMAC-SHA256(secret, b"sentinel/" + purpose).

        Browser side purposes derive from SESSION_SECRET, stored hash purposes from TOKEN_HASH_SECRET. An unknown
        purpose raises ValueError, so a typo can never silently share a key.
        """
        if purpose in SESSION_PURPOSES:
            secret = self.session_secret
        elif purpose in TOKEN_PURPOSES:
            secret = self.token_hash_secret
        else:
            raise ValueError(f"unknown key purpose: {purpose}")
        return hmac.new(secret, b"sentinel/" + purpose.encode(), hashlib.sha256).digest()


def is_loopback(host: str | None) -> bool:
    """True when a host name or address is this machine (localhost, 127.0.0.1 or ::1)."""
    return bool(host) and str(host).lower() in LOOPBACK_HOSTS


def _host_of(base_url: str) -> str | None:
    """The host part of a URL, or None when it cannot be parsed."""
    try:
        return urlsplit(base_url.strip()).hostname
    except ValueError:
        return None


def dev_mode(base_url: str | None, bind_host: str) -> bool:
    """True when both the public base URL's host and the bind host are loopback (a local run only)."""
    return base_url is not None and is_loopback(_host_of(base_url)) and is_loopback(bind_host)


def _value(env: Mapping[str, str], name: str) -> str | None:
    """An env var stripped of whitespace, or None when it is unset or empty."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def missing_env(env: Mapping[str, str], bind_host: str) -> list[str]:
    """Name every required variable that is unset or empty, in a stable order.

    Always DATABASE_URL, PUBLIC_BASE_URL, SESSION_SECRET, TOKEN_HASH_SECRET and ANTHROPIC_API_KEY (judge.py reads
    tasks and judges payments with Claude). RESEND_API_KEY and MAIL_FROM too
    unless in dev mode (with PUBLIC_BASE_URL missing, dev mode is judged from the bind host alone), and MAIL_FROM
    whenever RESEND_API_KEY is set. The wallet vars are added by mcp_server.main() from wallets.required_env().
    """
    missing = [name for name in REQUIRED_ENV if _value(env, name) is None]
    base_url = _value(env, "PUBLIC_BASE_URL")
    local = is_loopback(bind_host) if base_url is None else dev_mode(base_url, bind_host)
    if not local:
        missing += [name for name in MAIL_ENV if _value(env, name) is None]
    elif _value(env, "RESEND_API_KEY") is not None and _value(env, "MAIL_FROM") is None:
        missing.append("MAIL_FROM")
    return missing


def _normalize_base_url(raw: str, problems: list[str]) -> str | None:
    """Check PUBLIC_BASE_URL and return it as scheme://host[:port], appending every problem found."""
    found: list[str] = []
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        problems.append("PUBLIC_BASE_URL is not a valid URL")
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        found.append("PUBLIC_BASE_URL must start with https:// (or http:// on localhost)")
    if not host:
        found.append("PUBLIC_BASE_URL has no host")
    if "@" in parts.netloc:
        found.append("PUBLIC_BASE_URL must not contain a user name or password")
    if parts.path not in ("", "/"):
        found.append("PUBLIC_BASE_URL must not have a path (use https://host, the MCP URL is that plus /mcp)")
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        found.append("PUBLIC_BASE_URL must not have a query or fragment")
    if scheme == "http" and host and not is_loopback(host):
        found.append("PUBLIC_BASE_URL must use https outside localhost")
    if found:
        problems.extend(found)
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def load_settings(env: Mapping[str, str], bind_host: str) -> Settings:
    """Build Settings from the environment, or raise ConfigError naming every problem at once.

    Checks: PUBLIC_BASE_URL is http(s) with no userinfo, path, query or fragment, and https unless the host is
    loopback; both secrets are set, 32+ characters and different; outside dev mode RESEND_API_KEY and MAIL_FROM are
    set and SENTINEL_ALLOW_LOCAL_URLS is not; MAIL_FROM is set whenever a key is. FORWARDED_ALLOW_IPS="*", or unset
    outside dev mode, only warns.
    """
    problems: list[str] = []
    raw_base = _value(env, "PUBLIC_BASE_URL")
    base_url = None
    if raw_base is None:
        problems.append("PUBLIC_BASE_URL is not set")
    else:
        base_url = _normalize_base_url(raw_base, problems)
    local = dev_mode(raw_base, bind_host) if raw_base is not None else is_loopback(bind_host)

    secrets: dict[str, str] = {}
    for name in ("SESSION_SECRET", "TOKEN_HASH_SECRET"):
        value = _value(env, name)
        if value is None:
            problems.append(f"{name} is not set")
        elif len(value) < MIN_SECRET_LENGTH:
            problems.append(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
        else:
            secrets[name] = value
    if len(secrets) == 2 and secrets["SESSION_SECRET"] == secrets["TOKEN_HASH_SECRET"]:
        problems.append("SESSION_SECRET and TOKEN_HASH_SECRET must be different")

    api_key = _value(env, "RESEND_API_KEY")
    mail_from = _value(env, "MAIL_FROM")
    if not local:
        if api_key is None:
            problems.append("RESEND_API_KEY is required outside localhost")
        if mail_from is None:
            problems.append("MAIL_FROM is required outside localhost")
        if (env.get("SENTINEL_ALLOW_LOCAL_URLS") or "").strip() == "1":
            problems.append("SENTINEL_ALLOW_LOCAL_URLS=1 is only allowed on localhost")
    elif api_key is not None and mail_from is None:
        problems.append("MAIL_FROM is required when RESEND_API_KEY is set")

    warnings: list[str] = []
    forwarded = (env.get("FORWARDED_ALLOW_IPS") or "").strip()
    if forwarded == "*":
        warnings.append(
            'FORWARDED_ALLOW_IPS is "*": any client can spoof its IP through X-Forwarded-For, which defeats the '
            "per IP rate limits. Set it to your proxy's address."
        )
    elif not forwarded and not local:
        warnings.append(
            "FORWARDED_ALLOW_IPS is not set: behind a proxy every request seems to come from the proxy, so the per "
            "IP rate limits become site wide and the Anthropic registration exemption never matches. Set it to your "
            "proxy's address."
        )

    if problems or base_url is None:
        raise ConfigError(problems)
    return Settings(
        base_url=base_url,
        session_secret=secrets["SESSION_SECRET"].encode(),
        token_hash_secret=secrets["TOKEN_HASH_SECRET"].encode(),
        resend_api_key=api_key,
        mail_from=mail_from,
        bind_host=bind_host,
        dev_mode=local,
        warnings=tuple(warnings),
    )
