"""URL safety check for anything an agent asks Sentinel to fetch and pay for.

The server makes the request itself, with a funded wallet, to an address the agent chose. Without this check a
hosted Sentinel could be pointed at internal services (SSRF) or at cloud metadata endpoints. Used by mcp_server.py
before any request is made. Redirects are never followed by the payment client, so only the first URL matters.
"""
import ipaddress
import os
import socket
from urllib.parse import urlsplit


def allow_local() -> bool:
    """True when SENTINEL_ALLOW_LOCAL_URLS=1, for sellers running on this machine (local testing only)."""
    return os.environ.get("SENTINEL_ALLOW_LOCAL_URLS") == "1"


def resolve(host: str, port: int) -> list[str]:
    """Return every IP address a host name (or literal address) resolves to."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def validate_url(url: str, local_ok: bool | None = None) -> str:
    """Return the URL if it is safe to fetch, otherwise raise ValueError with a plain English reason.

    Allowed: http or https, no embedded credentials, and (unless local_ok) only public internet addresses.
    Every address the host resolves to must be public, which also catches numeric tricks like 2130706433.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs can be paid for.")
    if not parts.hostname:
        raise ValueError("The URL has no host.")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    if local_ok if local_ok is not None else allow_local():
        return url.strip()
    try:
        addresses = resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        raise ValueError(f"Could not find the host {parts.hostname}.") from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:127.0.0.1 is really 127.0.0.1
        if not ip.is_global:
            raise ValueError("That address is private or local, so Sentinel will not fetch it.")
    return url.strip()
