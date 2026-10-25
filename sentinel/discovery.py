"""Market discovery: search public x402 directories for services the agent could buy, without paying anything.

search() asks every source at once and returns one ranked list of Listings. Sources:
  cdp       Coinbase's x402 Bazaar (the facilitator's discovery index). The only source that lists Base Sepolia, and
            the only one that gives the seller's payTo, so the only one whose listings can be payable here.
  agentic   agentic.market, Coinbase's curated directory. Base mainnet.
  payapi    payapi.market, settlement verified listings. Base mainnet.
  apify     the Apify Store, Actors whitelisted for agentic payments. Paid through an Apify prepaid token on Base
            mainnet, not per call, so browse only.
A listing is payable only when it offers Base Sepolia USDC, the one option payments.py signs. Everything else is
returned as browse only so the agent can tell the owner what exists. Nothing here authorizes a payment: pay() still
fetches a fresh quote from the seller and the guard still blocks any seller the owner has not approved. Listings are
third party advertising text, so descriptions are cut short and stripped of markup before the agent sees them.

Pure normalizers (one per source) turn a JSON response into Listings and are tested on recorded responses; fetch()
does the network part and never raises for one source failing, it reports it in sources instead.
"""
import asyncio
import html
import json
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel

from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA

BASE_MAINNET = "eip155:8453"
CDP_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
AGENTIC_URL = "https://api.agentic.market/v1/services/search"
PAYAPI_URL = "https://payapi.market/agent/search"
APIFY_URL = "https://api.apify.com/v2/store"
SOURCES = ("cdp", "agentic", "payapi", "apify")

TIMEOUT_S = 8.0  # per source; a slow directory drops out instead of holding up the others
MAX_RESPONSE_BYTES = 2_000_000  # a directory answer bigger than this is ignored, not parsed
MAX_DESCRIPTION = 280
MAX_NAME = 80
DEFAULT_LIMIT, MAX_LIMIT = 10, 25
NETWORK_LABELS = {"base": BASE_MAINNET, "base-sepolia": NETWORK, "base sepolia": NETWORK}
BROWSE_ONLY = "Browse only: this listing does not offer Base Sepolia USDC, the only option Sentinel pays."
APIFY_CREDIT_NOTE = ("Runs on the owner's Apify credit, not x402. Call apify_actor(actor) for its pricing and the "
                     "inputs it needs. Use run_apify_actor with a task_id and max_charge_usd cap to buy results.")
APIFY_NOTE = ("Browse only: Apify Actors are paid through an Apify prepaid token (Base mainnet), not per call, so "
              "pay() cannot buy them. Tell the owner if this Actor fits the task.")

SellerStatus = Literal["approved", "blocked", "unlisted"]


class Listing(BaseModel):
    """One thing the agent could buy. amount is in USDC atomic units when the price is known."""

    source: str
    name: str
    description: str
    url: str
    method: str = "GET"
    price_usdc: str | None = None
    amount: int | None = None
    network: str | None = None
    pay_to: str | None = None
    query_params: dict[str, str] | None = None  # the seller's example inputs; most endpoints answer 400 without them
    example_url: str | None = None  # url with those example inputs filled in; change the values to fit the task
    payable: bool = False
    seller_status: SellerStatus | None = None  # only for payable listings: what pay() will find on the seller lists
    actor: str | None = None  # Apify only: the Actor id ("username/name") for apify_actor
    pay_with: Literal["x402", "apify_credit"] | None = None  # how it would be paid; None when it cannot be
    note: str | None = None


# Text cleanup


TAG = re.compile(r"<[^>]*>")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(text: Any, limit: int = MAX_DESCRIPTION) -> str:
    """Third party text made safe to show: markup and control characters gone, whitespace folded, cut to limit."""
    if not isinstance(text, str):
        return ""
    text = CONTROL.sub(" ", html.unescape(TAG.sub(" ", text)))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def to_atomic(dollars: Any) -> int | None:
    """A dollar amount ("0.10", 0.002) as USDC atomic units, rounded down; None when it is not a usable price."""
    try:
        value = Decimal(str(dollars))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value * 1_000_000)


def usdc_text(atomic: int) -> str:
    """Atomic units as a dollar string with at least two places: 500000 -> "0.50", 1000 -> "0.001"."""
    text = format((Decimal(atomic) / 1_000_000).normalize(), "f")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def network_id(label: Any) -> str | None:
    """A CAIP-2 network id from a directory's label ("Base" or "eip155:8453"), or None."""
    if not isinstance(label, str) or not label:
        return None
    return label if ":" in label else NETWORK_LABELS.get(label.strip().lower(), label.strip().lower())


def is_http_url(url: Any) -> bool:
    """True for an absolute http or https URL with a host. Anything else is dropped from the results."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def example_inputs(url: str, params: Any) -> tuple[dict[str, str] | None, str | None]:
    """The seller's example query parameters (cleaned, at most 10) and the url with them filled in."""
    if not isinstance(params, dict) or not params:
        return None, None
    cleaned = {clean(k, 40): clean(v if isinstance(v, str) else str(v), 100) for k, v in list(params.items())[:10]
               if isinstance(k, str) and isinstance(v, (str, int, float, bool))}
    if not cleaned:
        return None, None
    return cleaned, url + ("&" if urlsplit(url).query else "?") + urlencode(cleaned)


def priced(listing: Listing, amount: int | None) -> Listing:
    """Set amount and its dollar text together."""
    listing.amount = amount
    listing.price_usdc = usdc_text(amount) if amount is not None else None
    return listing


# One normalizer per source: JSON in, Listings out. Bad entries are skipped, never raised.


def from_cdp(data: Any) -> list[Listing]:
    """Bazaar search results. A resource is payable when one of its accepts is exact Base Sepolia USDC."""
    found = []
    for item in (data or {}).get("resources") or []:
        if not isinstance(item, dict) or not is_http_url(item.get("resource")):
            continue
        accepts = [a for a in item.get("accepts") or [] if isinstance(a, dict)]
        testnet = next((a for a in accepts if a.get("scheme") == "exact" and a.get("network") == NETWORK
                        and str(a.get("asset", "")).lower() == USDC_BASE_SEPOLIA.lower()), None)
        offer = testnet or next((a for a in accepts if a.get("network") == BASE_MAINNET), None) or (
            accepts[0] if accepts else None)
        info = ((item.get("extensions") or {}).get("bazaar") or {}).get("info") or {}
        method = str((info.get("input") or {}).get("method") or "GET").upper()
        url = item["resource"]
        params, example = example_inputs(url, (info.get("input") or {}).get("queryParams"))
        listing = Listing(
            source="cdp", name=clean(item.get("serviceName") or urlsplit(url).hostname, MAX_NAME),
            description=clean(item.get("description")), url=url, method=method,
            network=offer.get("network") if offer else None,
            pay_to=str(testnet["payTo"]) if testnet and testnet.get("payTo") else None,
            payable=bool(testnet and testnet.get("payTo") and method == "GET"),
            query_params=params, example_url=example,
        )
        if listing.payable:
            listing.pay_with = "x402"
        try:
            amount = int(offer["amount"]) if offer and str(offer.get("amount", "")).isdigit() else None
        except (TypeError, ValueError):
            amount = None
        if testnet and method != "GET":
            listing.note = f"Browse only: this endpoint needs a {method} request and pay() only makes GET requests."
        elif not listing.payable:
            listing.note = BROWSE_ONLY
        found.append(priced(listing, amount))
    return found


def from_agentic(data: Any) -> list[Listing]:
    """agentic.market services, one listing per priced endpoint. Prices are dollar strings; no payTo is given."""
    found = []
    for service in (data or {}).get("services") or []:
        if not isinstance(service, dict):
            continue
        for endpoint in service.get("endpoints") or []:
            if not isinstance(endpoint, dict) or not is_http_url(endpoint.get("url")):
                continue
            pricing = endpoint.get("pricing") or {}
            name = service.get("name") or endpoint.get("providerName") or urlsplit(endpoint["url"]).hostname
            listing = Listing(
                source="agentic", name=clean(name, MAX_NAME),
                description=clean(endpoint.get("description") or service.get("description")),
                url=endpoint["url"], method=str(endpoint.get("method") or "GET").upper(),
                network=network_id(pricing.get("network")), note=BROWSE_ONLY,
            )
            found.append(priced(listing, to_atomic(pricing.get("amount")) if pricing.get("amount") else None))
    return found


def from_payapi(data: Any) -> list[Listing]:
    """payapi.market results. Each is a provider; the listed URL is its base URL and the price is its lowest."""
    found = []
    for item in (data or {}).get("results") or []:
        if not isinstance(item, dict) or not is_http_url(item.get("base_url")):
            continue
        routes = [r for r in item.get("verified_routes") or [] if isinstance(r, dict)]
        listing = Listing(
            source="payapi", name=clean(item.get("name"), MAX_NAME), description=clean(item.get("description")),
            url=item["base_url"], method=str(routes[0].get("method") or "GET").upper() if routes else "GET",
            network=network_id(item.get("network_id") or item.get("network")), note=BROWSE_ONLY,
        )
        found.append(priced(listing, to_atomic(item["price_min"]) if item.get("price_min") is not None else None))
    return found


def from_apify(data: Any, credit: bool = False) -> list[Listing]:
    """Apify Store Actors, priced by their primary pay per event charge (free tier price).

    Without credit (no APIFY_TOKEN) only Actors whitelisted for agentic payments are kept, as browse only, since they
    need an x402 prepaid token on Base mainnet. With credit every Actor the owner's Apify account can run is kept and
    marked pay_with apify_credit, except monthly rentals (they need a subscription) and deprecated Actors.
    """
    found = []
    for actor in ((data or {}).get("data") or {}).get("items") or []:
        if not isinstance(actor, dict):
            continue
        pricing = actor.get("currentPricingInfo") or {}
        model = pricing.get("pricingModel") if isinstance(pricing, dict) else None
        if credit and (model == "FLAT_PRICE_PER_MONTH" or actor.get("isDeprecated")):
            continue
        if not credit and not actor.get("isWhiteListedForAgenticPayments"):
            continue
        url = actor.get("url") or f"https://apify.com/{actor.get('username')}/{actor.get('name')}"
        if not is_http_url(url):
            continue
        amount, event = primary_event_price(pricing)
        if credit and model == "FREE":
            amount = 0
        owner, name = actor.get("username"), actor.get("name")
        actor_id = f"{owner}/{name}" if owner and name else None
        if credit:
            per = (f" Price is per {event.lower()}, plus a small per run start fee." if event
                   else " Free Actor: only the run's compute is charged, usually well under $0.01 for a small run."
                   if model == "FREE" else "")
            note = APIFY_CREDIT_NOTE + per
        else:
            note = APIFY_NOTE + (f" Price is per {event.lower()}." if event else "")
        listing = Listing(
            source="apify", name=clean(actor.get("title") or actor.get("name"), MAX_NAME),
            description=clean(actor.get("description")), url=url, method="POST",
            network=None if credit else BASE_MAINNET, actor=actor_id,
            pay_with="apify_credit" if credit else None, note=note,
        )
        found.append(priced(listing, amount))
    return found


def primary_event_price(pricing: Any) -> tuple[int | None, str | None]:
    """The primary pay per event charge of an Apify pricing record: (atomic units at the free tier, event title)."""
    if not isinstance(pricing, dict):
        return None, None
    events = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents")
    for charge in events.values() if isinstance(events, dict) else []:
        if isinstance(charge, dict) and charge.get("isPrimaryEvent"):
            return event_price(charge), clean(charge.get("eventTitle"), 60) or None
    return None, None


def event_price(charge: dict[str, Any]) -> int | None:
    """One Apify charge event's price in atomic units: the FREE plan tier when tiered, else the flat price."""
    tiers = charge.get("eventTieredPricingUsd")
    price = (tiers.get("FREE") or {}).get("tieredEventPriceUsd") if isinstance(tiers, dict) else None
    if price is None:
        price = charge.get("eventPriceUsd")
    return to_atomic(price) if price is not None else None


# Fetching


Fetch = Callable[[str, dict[str, Any]], Awaitable[Any]]


def http_fetcher(client: httpx.AsyncClient, headers_for: Callable[[str], dict[str, str]] | None = None) -> Fetch:
    """A fetch function on one client: GET with params, JSON back, raising on errors or an oversized answer.

    headers_for adds per URL headers (apify.headers_for sends the owner's token to api.apify.com and nowhere else).
    """

    async def fetch(url: str, params: dict[str, Any]) -> Any:
        headers = {"Accept": "application/json", **(headers_for(url) if headers_for else {})}
        async with client.stream("GET", url, params=params, headers=headers) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("response too large")
        return json.loads(body)

    return fetch


async def from_source(source: str, fetch: Fetch, query: str, limit: int, max_amount: int | None,
                      payable_only: bool, apify_credit: bool = False) -> list[Listing]:
    """Ask one source and normalize its answer. Raises on any failure; search() turns that into a source error."""
    if source == "cdp":
        params: dict[str, Any] = {"query": query, "limit": min(limit, 20)}
        if max_amount is not None:
            params["maxUsdPrice"] = usdc_text(max_amount)
        # Testnet listings are rare in a mixed search, so ask for them on their own as well.
        testnet = fetch(CDP_URL, {**params, "network": NETWORK})
        if payable_only:
            return from_cdp(await testnet)
        both = await asyncio.gather(testnet, fetch(CDP_URL, params))
        return from_cdp(both[0]) + from_cdp(both[1])
    if source == "agentic":
        return from_agentic(await fetch(AGENTIC_URL, {"q": query, "limit": limit}))
    if source == "payapi":
        return from_payapi(await fetch(PAYAPI_URL, {"q": query, "limit": limit}))
    if source == "apify":
        # Without credit only some Actors (whitelisted for agentic payments) are kept, so ask for more.
        wanted = limit + 10 if apify_credit else min(limit * 3, 50)
        return from_apify(await fetch(APIFY_URL, {"search": query, "limit": wanted}), credit=apify_credit)
    raise ValueError(f"unknown source {source}")


def rank(listings: list[Listing]) -> list[Listing]:
    """One per URL, best first: payable and approved, payable and unlisted, then browse only, then blocked.

    Inside each group the sources' own relevance order is kept and the sources take turns, so one big directory
    (Apify has thousands of near free Actors) cannot push the others off the list.
    """
    seen: set[str] = set()
    unique = []
    for listing in sorted(listings, key=lambda x: not x.payable):  # stable: a payable copy of a URL wins
        if listing.url not in seen:
            seen.add(listing.url)
            unique.append(listing)

    def group(x: Listing) -> int:
        if not x.payable:
            return 2
        return {"approved": 0, "blocked": 3}.get(x.seller_status or "", 1)

    ranked: list[Listing] = []
    for g in range(4):
        by_source: dict[str, list[Listing]] = {}
        for listing in unique:
            if group(listing) == g:
                by_source.setdefault(listing.source, []).append(listing)
        queues = [by_source[s] for s in SOURCES if s in by_source]
        while queues:
            ranked += [q.pop(0) for q in queues]
            queues = [q for q in queues if q]
    return ranked


class Found(BaseModel):
    """What search() returns: the ranked listings and, per source, "ok" or why it gave nothing."""

    listings: list[Listing]
    sources: dict[str, str]


async def search(
    query: str,
    *,
    fetch: Fetch,
    max_amount: int | None = None,
    limit: int = DEFAULT_LIMIT,
    payable_only: bool = False,
    sources: tuple[str, ...] = SOURCES,
    seller_status: Callable[[list[str]], Awaitable[dict[str, SellerStatus]]] | None = None,
    apify_credit: bool = False,
) -> Found:
    """Search every source at once and return up to limit listings, best first.

    max_amount drops anything priced above it (atomic units); unpriced listings are kept only when there is no max.
    payable_only skips the browse only sources. seller_status maps payable payTo addresses (lower case) to what the
    seller lists say, so the agent knows before paying whether pay() will be blocked. apify_credit (the owner set
    APIFY_TOKEN) lists every runnable Apify Actor instead of only the x402 whitelisted ones.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    asked = tuple(s for s in sources if s == "cdp") if payable_only else sources
    outcomes = await asyncio.gather(
        *(asyncio.wait_for(from_source(s, fetch, query, limit, max_amount, payable_only, apify_credit), TIMEOUT_S)
          for s in asked),
        return_exceptions=True,
    )
    listings: list[Listing] = []
    status: dict[str, str] = {}
    for source, outcome in zip(asked, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            status[source] = "timed out" if isinstance(outcome, TimeoutError) else f"unavailable ({_why(outcome)})"
            continue
        status[source] = "ok"
        listings += outcome
    if max_amount is not None:
        listings = [x for x in listings if x.amount is not None and x.amount <= max_amount]
    if payable_only:
        listings = [x for x in listings if x.payable]
    payable = sorted({x.pay_to.lower() for x in listings if x.payable and x.pay_to})
    statuses = await seller_status(payable) if seller_status and payable else {}
    for listing in listings:
        if listing.payable and listing.pay_to:
            listing.seller_status = statuses.get(listing.pay_to.lower(), "unlisted")
    return Found(listings=rank(listings)[:limit], sources=status)


def _why(error: BaseException) -> str:
    """A short, URL free reason a source failed, for the agent and the logs."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.HTTPError):
        return type(error).__name__
    return "bad response" if isinstance(error, ValueError) else type(error).__name__
