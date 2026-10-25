"""Finding paid services for a task: a catalog in the x402 Bazaar discovery format, searched by keyword.

The agent asks Sentinel what it could buy (MCP tool find_services); Sentinel reads the catalog at
SENTINEL_DISCOVERY_URL (no default: find_services reports no catalog until one is set), ranks the listings against the
agent's query, and mcp_server.py previews each one against the task (payments.preview) so the agent sees which it
may buy before it buys anything. The catalog is seller written: names and descriptions are untrusted data. What a
listing advertises (price, wallet, description) is stored per task (mandates.remember_listings) so the intent rules
can catch a seller whose real 402 asks more, pays someone else, or sells something different.

Bazaar shape, as an x402 discovery endpoint serves it:
    {"x402Version": 2, "items": [{"resource": url, "type": "http", "accepts": [requirement, ...],
                                  "metadata": {"service_name": ..., "description": ..., "tags": [...]}}]}
"""
import asyncio
import logging
import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any

import httpx

from sentinel import db, guard, intent, live, mandates, payments, results, urls
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA

log = logging.getLogger("sentinel.discovery")
Connect = Callable[[], AbstractContextManager[db.Connection]]

ENV_VAR = "SENTINEL_DISCOVERY_URL"
MAX_RESULTS = 12
PREVIEWS_AT_ONCE = 4  # listings checked at the same time (each holds a pooled connection)
MAX_ITEMS = 200  # listings read from one catalog
TIMEOUT_S = 10.0
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset({"the", "and", "for", "with", "from", "data", "api", "buy", "need", "find", "get", "about", "into"})


class DiscoveryError(Exception):
    """The catalog could not be read. The message is safe to show the agent."""


@dataclass(frozen=True)
class Listing:
    """One paid service from the catalog. Everything but url is the seller's own claim."""

    url: str
    name: str
    description: str
    tags: tuple[str, ...]
    listed_amount: int | None  # Base Sepolia USDC price the listing claims, atomic units
    listed_pay_to: str | None = None  # the wallet the listing says payments go to


def catalog_url() -> str:
    """Where the catalog lives: SENTINEL_DISCOVERY_URL, or "" when none is set up."""
    return (os.environ.get(ENV_VAR) or "").strip()


def parse(document: Any) -> list[Listing]:
    """Turn a Bazaar discovery document into listings, skipping anything malformed. Seller text is clipped."""
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise DiscoveryError("The service catalog is not in the expected format.")
    listings = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict) or not isinstance(item.get("resource"), str):
            continue
        raw_meta = item.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_tags = meta.get("tags")
        tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
        listings.append(Listing(
            url=item["resource"],
            name=intent.clip(str(meta.get("service_name") or ""), 100) or "",
            description=intent.clip(str(meta.get("description") or "")) or "",
            tags=tuple(t for t in (intent.clip(str(tag), 40) for tag in tags[:10]) if t),
            listed_amount=_listed_amount(item.get("accepts")),
            listed_pay_to=_listed_pay_to(item.get("accepts")),
        ))
    return listings


def _usdc_option(accepts: Any) -> dict[str, Any] | None:
    """The listing's Base Sepolia USDC payment option, or None."""
    for option in accepts if isinstance(accepts, list) else []:
        if isinstance(option, dict) and option.get("network") == NETWORK \
                and str(option.get("asset", "")).lower() == USDC_BASE_SEPOLIA.lower():
            return option
    return None


def _listed_amount(accepts: Any) -> int | None:
    """The Base Sepolia USDC amount a listing claims, or None."""
    option = _usdc_option(accepts)
    try:
        return int(option["amount"]) if option is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _listed_pay_to(accepts: Any) -> str | None:
    """The wallet a listing says its Base Sepolia USDC payments go to, or None."""
    option = _usdc_option(accepts)
    pay_to = option.get("payTo") if option is not None else None
    return pay_to if isinstance(pay_to, str) and pay_to else None


async def fetch(url: str | None = None) -> list[Listing]:
    """Read the catalog. The caller has already checked the URL (urls.validate_url). Raises DiscoveryError."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as session:
            response = await session.get(url or catalog_url())
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError):
        raise DiscoveryError("Sentinel could not read the service catalog. Try again in a minute.") from None
    return parse(document)


def words(text: str) -> set[str]:
    """The search words in a text: lower case, 3+ characters, no filler words."""
    return {w for w in WORD.findall(text.lower()) if len(w) >= 3 and w not in STOP}


def search(listings: list[Listing], query: str, limit: int = MAX_RESULTS) -> list[Listing]:
    """The listings ranked by how many query words they mention (name, tags, description, URL), best first.

    Listings that match nothing still come after the matches, up to limit: Sentinel's verdicts tell the agent which
    ones it may not buy, so showing them is part of the answer.
    """
    wanted = words(query)

    def score(listing: Listing) -> int:
        have = words(" ".join((listing.name, listing.description, " ".join(listing.tags), listing.url)))
        return len(wanted & have)

    ranked = sorted(enumerate(listings), key=lambda pair: (-score(pair[1]), pair[0]))
    return [listing for _, listing in ranked[:limit]]


async def preview(
    connect: Connect, agent: str, task: payments.Task, url: str, purpose: str, listing: Listing | None = None,
) -> results.ServiceView:
    """What pay would decide for one URL under this task, as the agent sees it (check, below). Never pays."""
    name = listing.name if listing else None
    found = await check(connect, agent, task, url, purpose)
    if found.rule in (BAD_URL, UNREACHABLE):
        return results.service(url, found.verdict, found.detail, name=name)
    says = listing.description if listing else found.seller.description
    return results.service(url, found.verdict, found.detail, found.amount, name or found.seller.service_name, says)


BAD_URL, UNREACHABLE = "url", "unreachable"


async def check(connect: Connect, agent: str, task: payments.Task, url: str, purpose: str) -> payments.Preview:
    """payments.preview for one URL with its failures as verdicts: BLOCKED for a URL Sentinel will not fetch (rule
    url), UNREACHABLE when the seller could not be reached or checked (rule unreachable). A guard block is BLOCKED.

    The URL is checked first (urls.validate_url); the preview runs on a connection of its own.
    """
    try:
        safe_url = await asyncio.to_thread(urls.validate_url, url)
    except ValueError as e:
        return payments.Preview(url, "BLOCKED", BAD_URL, str(e))
    try:
        with connect() as conn, live.watching(conn, agent, task.mandate_id, safe_url, "Checking a listing", "search"):
            found = await payments.preview(conn, agent, safe_url, purpose, task)
    except httpx.HTTPError as e:
        log.warning("could not preview a service (%s)", type(e).__name__)
        return payments.Preview(url, "UNREACHABLE", UNREACHABLE, "Sentinel could not reach it.")
    except Exception:
        log.exception("preview hit an unexpected error")
        return payments.Preview(url, "UNREACHABLE", UNREACHABLE, "Sentinel could not check it.")
    return replace(found, verdict="BLOCKED") if found.verdict == guard.BLOCK else found


async def find(
    connect: Connect, agent: str, task: payments.Task, query: str, purpose: str,
) -> results.ServicesResult | list[results.ServiceView]:
    """Read the catalog, rank it against query and preview the best listings under this task.

    Returns every preview (results.services_found keeps only the buyable ones for the agent), or a FAILED result when
    the catalog cannot be read.
    """
    if not catalog_url():
        return results.ServicesResult(status="FAILED", message="No service catalog is set up on this Sentinel.")
    try:
        source = await asyncio.to_thread(urls.validate_url, catalog_url())
    except ValueError:
        return results.ServicesResult(status="FAILED", message="Sentinel's service catalog is not available here.")
    try:
        listings = search(await fetch(source), query)
    except DiscoveryError as e:
        return results.ServicesResult(status="FAILED", message=str(e))
    with connect() as conn:  # what was advertised, so the rules can compare each seller's real 402 against it
        mandates.remember_listings(conn, task.mandate_id, listings)
    gate = asyncio.Semaphore(PREVIEWS_AT_ONCE)

    async def one(listing: Listing) -> results.ServiceView:
        async with gate:
            return await preview(connect, agent, task, listing.url, purpose, listing)

    views = list(await asyncio.gather(*(one(listing) for listing in listings)))
    log.info("agent %s searched services for task %s: %s", agent, task.mandate_id,
             ", ".join(f"{v.name or v.url}={v.verdict}" for v in views))
    return views
