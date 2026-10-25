"""Price discovery: compare every service that could do one need of a task, and recommend the best value.

compare() runs the MCP tool compare_prices. It never pays:
  1. gather    Sentinel's catalog (catalog.py) and the public directories (discovery.py: the x402 Bazaar,
               agentic.market, payapi.market, the Apify Store) at once, keeping what matches the need: up to MAX_X402
               x402 offers Sentinel could pay, MAX_ACTORS Apify Actors and MAX_BROWSE browse only listings
  2. remember  what each payable listing advertised (mandates.remember_listings), so the bait and switch rules can
               compare the seller's real 402 against it
  3. check     each payable offer live: its own 402 price, then the guard and the intent rules exactly as pay would
               (catalog.check); each Actor's pricing and stats (apify.actor_details); our own earlier deliveries per
               product (attempts.deliveries)
  4. rate      one judge.rate call scores every offer Sentinel would allow for fit and coverage, price aside. Seller
               text reaches the model only as quoted JSON fields; a judge failure leaves every fit unknown, and an
               offer with an unknown fit is never recommended
  5. pick      pricing.pick: the best value, the best buyable and the cheapest buyable, stored per task
               (mandates.save_comparison) so the pricier_pick rule can block a purchase that costs more than the
               cheapest buyable one, unless the owner turned on auto buy and it is the recommended best value
Offers Sentinel stops (blocked, unreachable) are kept in the stored comparison and the owner's payment log,
not shown to the agent, as with find_services.
"""
import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any

from sentinel import (
    apify,
    attempts,
    catalog,
    chain,
    db,
    discovery,
    intent,
    judge,
    mandates,
    payments,
    pricing,
    results,
    urls,
)

log = logging.getLogger("sentinel.compare")
Connect = Callable[[], AbstractContextManager[db.Connection]]

MAX_X402, MAX_ACTORS, MAX_BROWSE = 8, 4, 4
CHECKS_AT_ONCE = 4  # live checks at the same time (each holds a pooled connection)
MAX_CALLS, MAX_RESULTS = 1000, 100_000
SHOWN_VERDICTS = (pricing.OK, pricing.FREE, pricing.APIFY, pricing.BROWSE)  # what the agent sees
KIND = {"call": "x402 data endpoint, charged per call", "result": "Apify Actor (a scraper or data tool), charged per "
        "result"}


@dataclass(frozen=True)
class Candidate:
    """One offer before pricing: what to buy (url, or the Actor), what the seller says, and its numbers."""

    offer: pricing.Offer
    url: str  # what pay() is called with (the seller's example inputs filled in), or the Actor's Store page
    says: str | None
    tags: tuple[str, ...] = ()
    actor: str | None = None
    listing: catalog.Listing | None = None  # what to remember for the bait and switch rules
    fit_reason: str | None = None
    note: str | None = None


def matches(listing: catalog.Listing, wanted: set[str]) -> int:
    """How many words of the need a catalog listing mentions (name, tags, description, URL)."""
    return len(wanted & catalog.words(" ".join((listing.name, listing.description, " ".join(listing.tags),
                                                 listing.url))))


def from_catalog(listings: list[catalog.Listing], query: str) -> list[Candidate]:
    """Catalog listings that mention the need, best match first."""
    wanted = catalog.words(query)
    hits = [x for x in catalog.search(listings, query, len(listings)) if matches(x, wanted)]
    return [Candidate(
        offer=pricing.Offer(key=intent.url_key(x.url), name=x.name or x.url, source="catalog", verdict=pricing.OK,
                            buyable=True, price=x.listed_amount),
        url=x.url, says=x.description or None, tags=x.tags, listing=x,
    ) for x in hits]


def from_directories(found: discovery.Found) -> list[Candidate]:
    """Directory listings: payable x402 ones (Base Sepolia, GET), Apify Actors, and browse only ones."""
    out = []
    for x in found.listings:
        if x.payable:
            url = x.example_url or x.url
            listing = catalog.Listing(url=url, name=x.name, description=x.description, tags=(),
                                      listed_amount=x.amount, listed_pay_to=x.pay_to)
            out.append(Candidate(pricing.Offer(key=intent.url_key(url), name=x.name, source=x.source,
                                               verdict=pricing.OK, buyable=True, price=x.amount),
                                 url=url, says=x.description or None, listing=listing))
        elif x.source == "apify" and x.actor:
            out.append(Candidate(pricing.Offer(key=x.actor, name=x.name, source="apify", verdict=pricing.APIFY,
                                               buyable=False, unit="result", price=x.amount),
                                 url=x.url, says=x.description or None, actor=x.actor, note=x.note))
        else:
            out.append(Candidate(pricing.Offer(key=intent.url_key(x.url), name=x.name, source=x.source,
                                               verdict=pricing.BROWSE, buyable=False, price=x.amount),
                                 url=x.url, says=x.description or None, note=x.note))
    return out


def pick_candidates(found: list[Candidate]) -> list[Candidate]:
    """At most MAX_X402 payable, MAX_ACTORS Actors and MAX_BROWSE browse only offers, first seen wins per key."""
    seen: set[str] = set()
    kept: list[Candidate] = []
    room = {pricing.OK: MAX_X402, pricing.APIFY: MAX_ACTORS, pricing.BROWSE: MAX_BROWSE}
    for c in found:
        if c.offer.key in seen or room[c.offer.verdict] <= 0:
            continue
        seen.add(c.offer.key)
        room[c.offer.verdict] -= 1
        kept.append(c)
    return kept


async def check_x402(connect: Connect, agent: str, task: payments.Task, c: Candidate, purpose: str) -> Candidate:
    """The seller's live price and what pay would decide, plus whether its wallet has a history."""
    found = await catalog.check(connect, agent, task, c.url, purpose)
    verdict = {payments.OK: pricing.OK, payments.FREE: pricing.FREE}.get(found.verdict, found.verdict)
    wallet_ok = False
    if found.pay_to and verdict == pricing.OK:
        with connect() as conn:  # cached for an hour; the preview just looked it up
            profile = await chain.cached_profile(conn, found.pay_to)
        wallet_ok = bool(profile and not (profile.flagged or profile.unused or profile.is_new()))
    offer = replace(c.offer, verdict=verdict, price=found.amount, live=found.amount is not None,
                    approved=verdict == pricing.OK, wallet_ok=wallet_ok)
    says = c.says or found.seller.description
    return replace(c, offer=offer, says=says, note=None if verdict in SHOWN_VERDICTS else found.detail)


async def check_actor(fetch: discovery.Fetch, c: Candidate) -> Candidate:
    """An Actor's full pricing (price per result, start fee) and its usage stats. The listing stands on failure."""
    try:
        details = await apify.actor_details(c.actor or "", fetch)
    except Exception:
        log.warning("could not read Apify Actor details (%s)", c.actor, exc_info=True)
        return c
    if details.status != "OK":
        return c
    price, start = c.offer.price, 0
    if details.pricing_model == "FREE":
        price = 0
    for event in details.events:
        amount = discovery.to_atomic(event.price_usdc) if event.price_usdc else None
        if amount is None:
            continue
        if event.primary:
            price = amount
        elif "start" in event.event.lower():
            start = amount
    if details.pricing_model == "FLAT_PRICE_PER_MONTH":
        price = None  # a rental needs a subscription, so it has no price per result
    offer = replace(c.offer, price=price, start_fee=start, success_rate=details.success_rate_30d,
                    rating=details.rating, users_30d=details.users_30d)
    return replace(c, offer=offer)


def rating_case(mandate: intent.Mandate, query: str, offers: list[Candidate]) -> dict[str, Any]:
    """The JSON document judge.rate reads: the task, the need, and each offer as quoted data, ids from 1. No prices."""
    return {
        "mandate": {"owner_task": mandate.task_text, "goal": mandate.goal,
                    "expected_purchase_types": list(mandate.expected_types),
                    "out_of_scope": list(mandate.out_of_scope)},
        "need": query,
        "offers": [{"id": i, "name": intent.clip(c.offer.name, 100), "seller_says": intent.clip(c.says),
                    "tags": [t for t in (intent.clip(tag, 40) for tag in c.tags[:10]) if t],
                    "kind": KIND[c.offer.unit]} for i, c in enumerate(offers, 1)],
    }


async def rate(the_judge: judge.Judge, mandate: intent.Mandate, query: str,
               offers: list[Candidate]) -> list[Candidate]:
    """Every offer with the judge's fit and coverage. On a judge failure every fit stays unknown (never recommended)."""
    if not offers:
        return offers
    try:
        sheet = await the_judge.rate(rating_case(mandate, query, offers))
    except judge.JudgeError:
        log.warning("the judge could not rate a price comparison")
        return offers
    by_id = {r.id: r for r in sheet.ratings}
    rated = []
    for i, c in enumerate(offers, 1):
        r = by_id.get(i)
        rated.append(c if r is None else replace(c, offer=replace(c.offer, fit=r.fit, coverage=r.coverage),
                                                 fit_reason=r.reason))
    return rated


def view(s: pricing.Scored, c: Candidate, recommended: bool) -> results.OfferView:
    """One scored offer as the agent sees it."""
    o = s.offer
    pay_with = "x402" if o.buyable else "apify_credit" if o.verdict == pricing.APIFY and apify.has_credit() else None
    return results.OfferView(
        name=o.name, source=o.source, url=c.url, actor=c.actor, pay_with=pay_with,  # type: ignore[arg-type]
        buyable=o.buyable and o.verdict in (pricing.OK, pricing.FREE),
        price_usdc=results.usdc_text(o.price) if o.price is not None else None, per=o.unit, price_checked=o.live,
        job_cost_usdc=results.usdc_text(s.cost) if s.cost is not None else None, quality=s.quality,
        fit_reason=c.fit_reason, seller_says=intent.clip(c.says, 280), recommended=recommended,
        note=s.why_not or c.note,
    )


def stored(s: pricing.Scored, c: Candidate, recommended: bool) -> dict[str, Any]:
    """One scored offer as the owner's Tasks page shows it, including the ones Sentinel stopped."""
    o = s.offer
    return {"name": o.name, "source": o.source, "url": c.url, "verdict": o.verdict, "buyable": o.buyable,
            "per": o.unit, "price": o.price, "live": o.live, "cost": s.cost, "quality": s.quality,
            "fit_reason": c.fit_reason, "why_not": s.why_not or c.note, "recommended": recommended}


async def compare(
    connect: Connect, agent: str, task: payments.Task, query: str, calls: int, results_wanted: int, *,
    fetch: discovery.Fetch, apify_credit: bool, purpose: str,
) -> results.ComparisonResult:
    """Compare every service for one need of this task and recommend the best value. Never pays.

    calls: how many x402 calls the job takes; results_wanted: how many results an Apify Actor would need to return.
    The caller has checked that the task is the agent's own.
    """
    calls, results_wanted = max(1, min(calls, MAX_CALLS)), max(1, min(results_wanted, MAX_RESULTS))
    with connect() as conn:
        aid = db.agent_id(conn, agent)
        row = mandates.get(conn, task.mandate_id, aid)
        if row is None:
            return results.ComparisonResult(status="FAILED", message="There is no such task for you.")
        mandate = mandates.to_mandate(row)
        budget_left = max(0, mandate.budget - mandates.spend(conn, mandate.id))
        auto_buy = bool(attempts.load_agent(conn, agent)["auto_buy_best_value"])

    async def read_catalog() -> list[catalog.Listing] | None:
        if not catalog.catalog_url():
            return None  # no catalog set up on this Sentinel: the directories alone
        source = await asyncio.to_thread(urls.validate_url, catalog.catalog_url())
        return await catalog.fetch(source)

    own, directories = await asyncio.gather(
        read_catalog(),
        discovery.search(query, fetch=fetch, limit=discovery.MAX_LIMIT, apify_credit=apify_credit),
        return_exceptions=True,
    )
    sources: dict[str, str] = {}
    found: list[Candidate] = []
    if isinstance(own, BaseException):
        sources["catalog"] = "unavailable"
    elif own is None:
        sources["catalog"] = "not set up"
    else:
        sources["catalog"] = "ok"
        found += from_catalog(own, query)
    if isinstance(directories, BaseException):
        log.warning("directory search failed in a price comparison", exc_info=directories)
        sources.update({s: "unavailable" for s in discovery.SOURCES})
    else:
        sources.update(directories.sources)
        found += from_directories(directories)
    candidates = pick_candidates(found)
    if not candidates:
        return results.ComparisonResult(status="NONE_FOUND", sources=sources, calls=calls, results=results_wanted,
                                        message="Nothing matched. Try broader words.")

    with connect() as conn:  # what was advertised, so the rules can compare each seller's real 402 against it
        mandates.remember_listings(conn, mandate.id, [c.listing for c in candidates if c.listing is not None])
    gate = asyncio.Semaphore(CHECKS_AT_ONCE)

    async def one(c: Candidate) -> Candidate:
        async with gate:
            if c.offer.verdict == pricing.OK:
                return await check_x402(connect, agent, task, c, purpose)
            if c.offer.verdict == pricing.APIFY:
                return await check_actor(fetch, c)
            return c

    checked = list(await asyncio.gather(*(one(c) for c in candidates)))
    with connect() as conn:
        delivered = attempts.deliveries(conn, agent, [c.offer.key for c in checked if c.offer.buyable])
    checked = [replace(c, offer=replace(c.offer, delivery=pricing.Delivery(*delivered[c.offer.key])))
               if c.offer.key in delivered else c for c in checked]
    allowed = [c for c in checked if c.offer.verdict in SHOWN_VERDICTS]
    rated = await rate(task.judge, mandate, query, allowed)
    every = rated + [c for c in checked if c.offer.verdict not in SHOWN_VERDICTS]
    by_key = {c.offer.key: c for c in every}
    chosen = pricing.pick([c.offer for c in every], calls, results_wanted, budget_left)
    explanation = pricing.explain(chosen)

    def recommended(s: pricing.Scored) -> bool:
        return any(s is p for p in (chosen.best, chosen.best_buyable) if p is not None)

    def summary(s: pricing.Scored | None) -> tuple[str, str, int] | None:
        return (s.offer.key, s.offer.name, s.cost) if s is not None and s.cost is not None else None

    buyable_keys = [s.offer.key for s in chosen.scored if s.offer.buyable]
    with connect() as conn, db.transaction(conn):
        comparison_id = mandates.save_comparison(
            conn, mandate.id, query, calls, results_wanted,
            [stored(s, by_key[s.offer.key], recommended(s)) for s in chosen.scored], buyable_keys,
            summary(chosen.best_buyable), summary(chosen.cheapest_buyable), explanation,
        )
    shown = [view(s, by_key[s.offer.key], recommended(s)) for s in chosen.scored
             if s.offer.verdict in SHOWN_VERDICTS]

    def as_view(s: pricing.Scored | None) -> results.OfferView | None:
        return view(s, by_key[s.offer.key], True) if s is not None else None

    log.info("agent %s compared %r for task %s: %d offers, best %s, cheapest %s", agent, query, mandate.id,
             len(chosen.scored), chosen.best_buyable and chosen.best_buyable.offer.key,
             chosen.cheapest_buyable and chosen.cheapest_buyable.offer.key)
    outcome = results.ComparisonResult(
        status="OK", message="", comparison_id=comparison_id, calls=calls, results=results_wanted,
        best=as_view(chosen.best), best_buyable=as_view(chosen.best_buyable),
        cheapest_buyable=view(chosen.cheapest_buyable, by_key[chosen.cheapest_buyable.offer.key],
                              recommended(chosen.cheapest_buyable)) if chosen.cheapest_buyable else None,
        needs_owner=chosen.pricier, offers=shown, sources=sources,
    )
    return results.compared(outcome, explanation, auto_buy)