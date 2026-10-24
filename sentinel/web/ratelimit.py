"""Rate limits for sign in links and client registration, counted from the rows those actions already write.

There is no counter table: check_link_limits counts magic_links rows and check_registration_limits counts
oauth_clients rows inside a time window decided by SQL now(), so every app replica shares the same limits and tests
age rows with an UPDATE. Each check takes transaction level advisory locks (the IP, then the address, then one site
wide lock) so two requests cannot both slip under a limit; the caller runs it in the same db.transaction as the insert
it guards (the check refuses to run outside one), and when a limit is reached nothing is inserted.

IPs are never stored: rows carry ip_hash, an HMAC of network_key(ip), where IPv4 mapped IPv6 is unwrapped and other
IPv6 is grouped by /64 for sign in links (one home or one server usually owns a whole /64) and by /48 for
registrations (a cheap server gets a /48 or more, and registering is the cheaper thing to flood). Registrations from
Anthropic's egress range are exempt from the per IP limit, because every claude.ai user registers from there; they
are stored with EXEMPT_MARK in front of their hash and have an overall budget of their own, so a flood from anywhere
else cannot use up the budget claude.ai users need (and the reverse).
"""
import ipaddress
import math
import os

import psycopg

from sentinel.db import Connection
from sentinel.web import Settings, link_limit_factor
from sentinel.web.auth import digest

ANTHROPIC_EGRESS = ipaddress.ip_network("160.79.104.0/21")  # every claude.ai user registers from here

# (interval, limit): links per address, per IP and overall; registrations per IP and overall.
LINKS_PER_ADDRESS = (("15 minutes", 3), ("1 day", 10))
LINKS_PER_IP = ("1 hour", 60)
LINKS_OVERALL = ("1 hour", 300)
REGISTRATIONS_PER_IP = ("1 hour", 20)
REGISTRATIONS_OVERALL = ("1 hour", 500)  # non exempt registrations
EXEMPT_REGISTRATIONS_OVERALL = ("1 hour", 500)  # registrations from ANTHROPIC_EGRESS, counted apart
LINK_V6_PREFIX = 64
REGISTRATION_V6_PREFIX = 48
EXEMPT_MARK = "exempt:"  # prefix of oauth_clients.created_ip_hash for exempt registrations (a hex hash never has it)


class RateLimited(Exception):
    """A limit was reached. retry_after is how many seconds until it frees up (at least 1)."""

    def __init__(self, retry_after: int) -> None:
        """Keep the wait in whole seconds, never less than 1."""
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"rate limited; retry after {self.retry_after} s")


def _address(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP, unwrapping IPv4 mapped IPv6 and dropping any IPv6 zone; None when it does not parse."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        return ipaddress.IPv6Address(int(addr))  # drops a %zone
    return addr


def network_key(ip: str, v6_prefix: int = LINK_V6_PREFIX) -> str:
    """What one client counts as: the IPv4 address, or its IPv6 network ("2001:db8:1:2::/64" by default);
    "unknown" if unparsable."""
    addr = _address(ip)
    if addr is None:
        return "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        drop = 128 - v6_prefix
        return str(ipaddress.IPv6Network((int(addr) >> drop << drop, v6_prefix)))
    return str(addr)


def ip_hash(settings: Settings, ip: str, *, v6_prefix: int = LINK_V6_PREFIX) -> str:
    """The stored form of a client's IP: an HMAC of its network key. The IP itself is never stored."""
    return digest(settings.key("ip"), network_key(ip, v6_prefix))


def registration_ip_hash(settings: Settings, ip: str) -> str:
    """The IP hash a client registration is limited by: IPv6 grouped by /48."""
    return ip_hash(settings, ip, v6_prefix=REGISTRATION_V6_PREFIX)


def stored_registration_hash(ip_hash: str, exempt: bool) -> str:
    """What oauth_clients.created_ip_hash holds: the hash, with EXEMPT_MARK in front for an exempt registration."""
    return EXEMPT_MARK + ip_hash if exempt else ip_hash


def is_exempt(ip: str) -> bool:
    """True for an IPv4 address (or IPv4 mapped IPv6) inside Anthropic's egress range."""
    addr = _address(ip)
    return isinstance(addr, ipaddress.IPv4Address) and addr in ANTHROPIC_EGRESS


def _require_transaction(conn: Connection) -> None:
    """Refuse to count outside a transaction: in autocommit the lock would end with its own statement."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
        raise RuntimeError("rate limit checks must run inside db.transaction")


def _lock(conn: Connection, scope: str, value: str) -> None:
    """Take a transaction level advisory lock on one scoped value (released at commit or rollback)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (f"sentinel/{scope}/{value}",))


def _wait(conn: Connection, table: str, condition: str, values: tuple[str, ...], interval: str, limit: int) -> int:
    """Seconds until this window drops below its limit, or 0 when it is already below.

    Counts rows of table created within interval of now() that match condition (an SQL fragment with %s
    placeholders for values; "" counts every row). When the count has reached the limit, the wait is until enough
    of the oldest counted rows leave the window for one more to fit: with exactly limit rows, that is the oldest one.
    """
    where = "created_at > now() - %s::interval"
    params: tuple[str, ...] = (interval,)
    if condition:
        where = f"{condition} AND " + where
        params = (*values, interval)
    row = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params).fetchone()
    count = int(row["n"]) if row else 0
    if count < limit:
        return 0
    row = conn.execute(
        f"SELECT extract(epoch FROM created_at + %s::interval - now()) AS wait FROM {table} WHERE {where}"
        " ORDER BY created_at OFFSET %s LIMIT 1",
        (interval, *params, count - limit),
    ).fetchone()
    return max(1, math.ceil(float(row["wait"]))) if row else 1


def check_link_limits(conn: Connection, *, limit_key: str, ip_hash: str) -> None:
    """Refuse a new sign in link when its address, its IP or the whole site has sent too many.

    3 per 15 minutes and 10 per day per limit_key, 60 per hour per ip_hash, 300 per hour overall (all multiplied by
    SENTINEL_LINK_LIMIT_FACTOR when a tester sets it, 1 otherwise). Call inside the
    same db.transaction as auth.issue_link. Raises RateLimited with the longest wait among the limits reached.
    """
    _require_transaction(conn)
    _lock(conn, "link-ip", ip_hash)
    _lock(conn, "link-address", limit_key)
    _lock(conn, "link-all", "")  # last, so the overall count is exact too
    factor = link_limit_factor(os.environ)  # 1 unless a tester raised it (see web.LINK_LIMIT_FACTOR_VAR)
    waits = [
        _wait(conn, "magic_links", "limit_key = %s", (limit_key,), interval, n * factor)
        for interval, n in LINKS_PER_ADDRESS
    ]
    waits.append(_wait(conn, "magic_links", "ip_hash = %s", (ip_hash,), LINKS_PER_IP[0], LINKS_PER_IP[1] * factor))
    waits.append(_wait(conn, "magic_links", "", (), LINKS_OVERALL[0], LINKS_OVERALL[1] * factor))
    if max(waits) > 0:
        raise RateLimited(max(waits))


def check_registration_limits(conn: Connection, *, ip_hash: str, exempt: bool) -> None:
    """Refuse a new OAuth client registration when its IP or its overall budget has made too many.

    Not exempt: 20 per hour per ip_hash (registration_ip_hash, IPv6 by /48) and 500 per hour across every non exempt
    registration. Exempt: no per IP limit, and 500 per hour across exempt registrations only. Counted over
    oauth_clients, whose created_ip_hash the caller writes with stored_registration_hash. Call inside the same
    db.transaction as the insert. Raises RateLimited with the longest wait among the limits reached.
    """
    _require_transaction(conn)
    if exempt:
        _lock(conn, "register-all-exempt", "")
        waits = [_wait(conn, "oauth_clients", "starts_with(created_ip_hash, %s)", (EXEMPT_MARK,),
                       *EXEMPT_REGISTRATIONS_OVERALL)]
    else:
        _lock(conn, "register-ip", ip_hash)
        _lock(conn, "register-all", "")  # last, so the overall count is exact too
        waits = [
            _wait(conn, "oauth_clients", "NOT starts_with(created_ip_hash, %s)", (EXEMPT_MARK,),
                  *REGISTRATIONS_OVERALL),
            _wait(conn, "oauth_clients", "created_ip_hash = %s", (ip_hash,), *REGISTRATIONS_PER_IP),
        ]
    if max(waits) > 0:
        raise RateLimited(max(waits))
