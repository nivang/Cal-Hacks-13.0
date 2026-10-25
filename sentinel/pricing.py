"""Price discovery: which of the services that could do a job is the best value. Pure, like guard.py.

compare.py gathers the offers (every x402 seller and Apify Actor that matches the need, each x402 price read live from
the seller's own 402 and checked by the guard and intent rules) and judge.rate scores how well each fits the task.
This module turns that into one decision, with no network, database or model:
  cost     what the job would cost: price x calls for an x402 seller, start fee + price x results for an Apify Actor
  quality  0 to 100 points: fit to the task (40), how much of the need it covers (30), track record (20: what our own
           earlier purchases delivered, else Apify's success rate and rating), trust (10: approved seller, used wallet)
  pick     start from the cheapest offer that may be recommended and move to a pricier one only when it scores at least
           compare_quality_margin points more than the current pick, so a pricier offer must be clearly better
An offer may be recommended only when Sentinel would let it through (verdict OK), its fit is known and at least
compare_min_fit, its price is known and, for x402, it fits what is left of the task's budget. Browse only listings
(mainnet, price not checked) are shown, never recommended. Apify Actors can be recommended but not bought yet, so the
best buyable offer is picked as well, and that is the one the pricier_pick rule (intent.py) holds for the owner when
it costs more than the cheapest buyable one. Amounts are integer atomic units; scores are integers.
"""
from dataclasses import dataclass
from typing import Literal

from sentinel import guard

OK = "OK"  # verdicts from compare.py: OK, BLOCKED, FREE, UNREACHABLE (x402) or APIFY, BROWSE
APIFY, BROWSE, FREE = "APIFY", "BROWSE", "FREE"
Unit = Literal["call", "result"]


@dataclass(frozen=True)
class Delivery:
    """What our own earlier purchases of this URL delivered: settled, settled but empty, and failed to deliver."""

    settled: int
    empty: int
    failed: int


@dataclass(frozen=True)
class Offer:
    """One service that could do the job. Everything the seller wrote stays in compare.py; this is numbers and flags."""

    key: str  # the URL without its query (x402) or the Actor id (Apify)
    name: str
    source: str
    verdict: str
    buyable: bool  # pay() can buy it today: x402, Base Sepolia USDC, GET
    unit: Unit = "call"
    price: int | None = None  # per call or per result, atomic units
    start_fee: int = 0  # Apify: charged once per run
    live: bool = False  # the price came from the seller's 402 just now, not from a listing
    fit: int | None = None  # 0 to 10 from judge.rate; None when the judge could not rate it
    coverage: int | None = None  # 0 to 10: how much of the need it covers
    delivery: Delivery | None = None
    success_rate: float | None = None  # Apify, last 30 days
    rating: float | None = None  # Apify, 0 to 5
    users_30d: int | None = None
    approved: bool = False  # x402: the seller is on an approved list
    wallet_ok: bool = False  # x402: the payee's wallet has a history and is not flagged


@dataclass(frozen=True)
class Scored:
    """An offer with its cost for this job, its quality and whether it may be recommended (else why not)."""

    offer: Offer
    cost: int | None
    quality: int | None
    eligible: bool
    why_not: str | None = None


@dataclass(frozen=True)
class Pick:
    """What compare_prices recommends. best may be an Apify Actor; best_buyable and cheapest_buyable are x402 only."""

    scored: tuple[Scored, ...]  # best first: eligible by quality per the pick order, then the rest
    best: Scored | None
    best_buyable: Scored | None
    cheapest_buyable: Scored | None

    @property
    def pricier(self) -> bool:
        """True when the best buyable offer costs more than the cheapest buyable one: pay blocks it without auto buy."""
        return bool(self.best_buyable and self.cheapest_buyable and self.best_buyable.cost is not None
                    and self.cheapest_buyable.cost is not None
                    and self.best_buyable.cost > self.cheapest_buyable.cost)


def cost(offer: Offer, calls: int, results: int) -> int | None:
    """What this job would cost with this offer, in atomic units, or None when its price is unknown."""
    if offer.verdict == FREE:
        return 0
    if offer.price is None:
        return None
    if offer.unit == "result":
        return offer.start_fee + offer.price * results
    return offer.price * calls


def track_points(offer: Offer) -> int:
    """0 to 20 for track record: our own deliveries first (an empty answer counts as a failure), then Apify's stats.

    Nothing known scores the middle, 10, so a new seller is neither rewarded nor punished.
    """
    d = offer.delivery
    if d is not None and d.settled + d.failed > 0:
        return round(20 * max(0, d.settled - d.empty) / (d.settled + d.failed))
    if offer.success_rate is not None or offer.rating is not None:
        success = offer.success_rate if offer.success_rate is not None else 0.5
        rating = offer.rating / 5 if offer.rating is not None else success
        return round(20 * min(1.0, max(0.0, 0.7 * success + 0.3 * rating)))
    return 10


def trust_points(offer: Offer) -> int:
    """0 to 10: an approved seller and a wallet with a history (x402), or an Actor many people use (Apify)."""
    if offer.unit == "result" or offer.verdict == APIFY:
        users = offer.users_30d or 0
        return 10 if users >= 100 else 5 if users >= 10 else 2
    return 5 * offer.approved + 5 * offer.wallet_ok


def quality(offer: Offer) -> int | None:
    """0 to 100 points for how good this offer is for the task, price aside. None when its fit is unknown."""
    if offer.fit is None:
        return None
    fit = max(0, min(10, offer.fit))
    coverage = max(0, min(10, offer.coverage if offer.coverage is not None else fit))
    return 4 * fit + 3 * coverage + track_points(offer) + trust_points(offer)


def score(offer: Offer, calls: int, results: int, budget_left: int, policy: dict[str, int] = guard.POLICY) -> Scored:
    """Cost, quality and whether the offer may be recommended. why_not is a plain English reason when it may not."""
    total, points = cost(offer, calls, results), quality(offer)
    why_not = None
    if offer.verdict == BROWSE:
        why_not = "Sentinel cannot pay it (not Base Sepolia USDC) and its price was not checked."
    elif offer.verdict not in (OK, FREE, APIFY):
        why_not = "Sentinel would not pay it for this task."
    elif points is None or offer.fit is None:
        why_not = "Sentinel could not rate how well it fits the task."
    elif offer.fit * 10 < policy["compare_min_fit"]:
        why_not = "It does not fit the task well enough."
    elif total is None:
        why_not = "Its price is not known."
    elif offer.buyable and total > budget_left:
        why_not = f"It would cost {guard.usd(total)}, more than the {guard.usd(budget_left)} left in the task's budget."
    return Scored(offer, total, points, why_not is None, why_not)


def choose(candidates: list[Scored], margin: int) -> Scored | None:
    """The best value: the cheapest candidate, replaced by a pricier one each time it scores margin points more."""
    ordered = sorted(candidates, key=lambda s: (s.cost or 0, -(s.quality or 0), s.offer.key))
    if not ordered:
        return None
    current = ordered[0]
    for candidate in ordered[1:]:
        if (candidate.quality or 0) >= (current.quality or 0) + margin:
            current = candidate
    return current


def pick(offers: list[Offer], calls: int, results: int, budget_left: int,
         policy: dict[str, int] = guard.POLICY) -> Pick:
    """Score every offer and pick the best value overall, the best buyable and the cheapest buyable.

    Duplicates (same key) keep the copy with a live price. The returned order puts the recommendable offers first, best
    value first, then the rest by quality.
    """
    unique: dict[str, Offer] = {}
    for offer in offers:
        if offer.key not in unique or (offer.live and not unique[offer.key].live):
            unique[offer.key] = offer
    scored = [score(o, max(1, calls), max(1, results), budget_left, policy) for o in unique.values()]
    eligible = [s for s in scored if s.eligible]
    buyable = [s for s in eligible if s.offer.buyable or s.offer.verdict == FREE]
    margin = policy["compare_quality_margin"]
    best_buyable = choose(buyable, margin)
    best_actor = choose([s for s in eligible if s.offer.verdict == APIFY], margin)
    # The overall best is one of the two finalists, picked the same way, so it never contradicts best_buyable.
    best = choose([s for s in (best_buyable, best_actor) if s is not None], margin)
    cheapest = min(buyable, key=lambda s: (s.cost or 0, -(s.quality or 0), s.offer.key)) if buyable else None
    first = [s for s in (best, best_buyable) if s is not None]
    rest = sorted((s for s in scored if all(s is not f for f in first)),
                  key=lambda s: (not s.eligible, -(s.quality if s.quality is not None else -1), s.cost or 0))
    order = list(dict.fromkeys(first)) + rest
    return Pick(tuple(order), best, best_buyable, cheapest)


def explain(pick_: Pick) -> str:
    """One or two sentences for the owner and the agent: what is recommended and, when it is pricier, why."""
    best, cheap = pick_.best_buyable, pick_.cheapest_buyable
    if best is None or best.cost is None:
        return "Nothing Sentinel can buy fits this task."
    text = f"Best value: {best.offer.name} at {guard.usd(best.cost)} for this job, scoring {best.quality} of 100."
    if pick_.pricier and cheap is not None and cheap.cost is not None:
        text += (f" It costs more than {cheap.offer.name} ({guard.usd(cheap.cost)}, scoring {cheap.quality}) and is "
                 f"picked because it scores {(best.quality or 0) - (cheap.quality or 0)} points higher.")
    return text
