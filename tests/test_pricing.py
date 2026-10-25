"""Answer key for pricing.py: what a job costs, how an offer scores, and which offer is the best value.

Pure: no database, no network, no model.
"""
from dataclasses import replace

from sentinel import guard, pricing

POLICY = guard.POLICY
MARGIN = POLICY["compare_quality_margin"]
BIG_BUDGET = 100_000_000

# An approved x402 seller with a used wallet, rated by the judge 8 fit and 8 coverage: 32 + 24 + 10 + 10 = 76.
CHEAP = pricing.Offer(key="http://a.test/hotels", name="A Hotels", source="catalog", verdict=pricing.OK, buyable=True,
                      price=50_000, live=True, fit=8, coverage=8, approved=True, wallet_ok=True)


def offer(key: str, price: int, fit: int = 8, coverage: int = 8, **changes: object) -> pricing.Offer:
    return replace(CHEAP, key=key, name=key, price=price, fit=fit, coverage=coverage, **changes)  # type: ignore[arg-type]


def pick(*offers: pricing.Offer, calls: int = 1, results: int = 50, budget: int = BIG_BUDGET) -> pricing.Pick:
    return pricing.pick(list(offers), calls, results, budget, POLICY)


# Cost


def test_an_x402_job_costs_the_price_times_the_calls():
    assert pricing.cost(CHEAP, 3, 50) == 150_000


def test_an_apify_job_costs_the_start_fee_plus_the_price_per_result():
    actor = replace(CHEAP, unit="result", price=2_000, start_fee=5_000, verdict=pricing.APIFY, buyable=False)
    assert pricing.cost(actor, 3, 100) == 5_000 + 200_000


def test_a_free_url_costs_nothing_and_an_unknown_price_is_none():
    assert pricing.cost(replace(CHEAP, verdict=pricing.FREE, price=None), 5, 5) == 0
    assert pricing.cost(replace(CHEAP, price=None), 1, 1) is None


# Quality


def test_quality_adds_fit_coverage_track_record_and_trust():
    assert pricing.quality(CHEAP) == 4 * 8 + 3 * 8 + 10 + 10


def test_unknown_fit_means_unknown_quality():
    assert pricing.quality(replace(CHEAP, fit=None)) is None


def test_missing_coverage_counts_as_the_fit():
    assert pricing.quality(replace(CHEAP, coverage=None)) == pricing.quality(CHEAP)


def test_our_own_deliveries_set_the_track_record_and_an_empty_answer_counts_as_a_failure():
    good = replace(CHEAP, delivery=pricing.Delivery(settled=4, empty=0, failed=0))
    poor = replace(CHEAP, delivery=pricing.Delivery(settled=4, empty=1, failed=4))
    assert pricing.track_points(good) == 20
    assert pricing.track_points(poor) == round(20 * 3 / 8)


def test_apify_stats_set_the_track_record_when_we_never_bought_from_it():
    actor = replace(CHEAP, verdict=pricing.APIFY, unit="result", success_rate=1.0, rating=5.0, users_30d=500)
    assert pricing.track_points(actor) == 20
    assert pricing.trust_points(actor) == 10
    assert pricing.trust_points(replace(actor, users_30d=3)) == 2


def test_an_unknown_track_record_scores_the_middle():
    assert pricing.track_points(CHEAP) == 10


def test_an_unapproved_seller_with_a_fresh_wallet_gets_no_trust_points():
    assert pricing.trust_points(replace(CHEAP, approved=False, wallet_ok=False)) == 0


# Picking


def test_the_cheapest_wins_when_the_pricier_one_is_not_clearly_better():
    pricier = offer("http://b.test/hotels", 150_000, fit=9, coverage=9)  # 7 points more, under the margin
    result = pick(CHEAP, pricier)
    assert result.best_buyable is not None and result.best_buyable.offer.key == CHEAP.key
    assert not result.pricier


def test_a_pricier_offer_wins_when_it_scores_at_least_the_margin_more():
    rich = offer("http://b.test/hotels", 150_000, fit=10, coverage=10)  # 40 + 30 + 20 = 90 vs 76, under 15
    assert not pick(CHEAP, rich).pricier
    thin = replace(CHEAP, coverage=3)  # 32 + 9 + 20 = 61, so 90 is 29 more
    result = pick(thin, rich)
    assert result.best_buyable is not None and result.best_buyable.offer.key == rich.key
    assert result.cheapest_buyable is not None and result.cheapest_buyable.offer.key == thin.key
    assert result.pricier
    assert "costs more than" in pricing.explain(result) and "29 points higher" in pricing.explain(result)


def test_the_pick_climbs_step_by_step_and_each_step_must_clear_the_margin():
    a = offer("http://a.test/x", 10_000, fit=5, coverage=5)  # 20 + 15 + 20 = 55
    b = offer("http://b.test/x", 20_000, fit=8, coverage=8)  # 76: 21 more than a, picked
    c = offer("http://c.test/x", 30_000, fit=9, coverage=10)  # 36 + 30 + 20 = 86: only 10 more than b
    result = pick(a, b, c)
    assert result.best_buyable is not None and result.best_buyable.offer.key == b.key


def test_blocked_held_and_unreachable_offers_are_never_recommended():
    for verdict in ("BLOCKED", "NEEDS_OWNER", "UNREACHABLE"):
        great = offer("http://x.test/a", 10_000, fit=10, coverage=10, verdict=verdict)
        result = pick(great)
        assert result.best is None and result.best_buyable is None
        assert result.scored[0].why_not == "Sentinel would not pay it for this task."


def test_an_off_task_offer_is_never_recommended_however_cheap():
    off = offer("http://x.test/a", 1_000, fit=4, coverage=10)
    result = pick(off, CHEAP)
    assert result.best_buyable is not None and result.best_buyable.offer.key == CHEAP.key
    assert next(s for s in result.scored if s.offer.key == off.key).why_not == "It does not fit the task well enough."


def test_an_offer_the_judge_could_not_rate_is_never_recommended():
    unrated = offer("http://x.test/a", 1_000, fit=None)
    result = pick(unrated, CHEAP)
    assert result.best_buyable is not None and result.best_buyable.offer.key == CHEAP.key


def test_an_offer_over_the_tasks_remaining_budget_is_never_recommended():
    result = pick(offer("http://b.test/x", 400_000), calls=2, budget=500_000)
    assert result.best_buyable is None
    assert "more than the $0.50 left" in (result.scored[0].why_not or "")


def test_browse_only_listings_are_shown_but_never_recommended():
    browse = offer("https://mainnet.test/x", 1_000, fit=10, coverage=10, verdict=pricing.BROWSE, buyable=False)
    result = pick(browse, CHEAP)
    assert result.best is not None and result.best.offer.key == CHEAP.key
    assert result.scored[-1].offer.key == browse.key and not result.scored[-1].eligible


def test_an_apify_actor_can_be_the_best_value_but_the_best_buyable_stays_x402():
    actor = replace(CHEAP, key="apify/hotels", name="Hotel Actor", verdict=pricing.APIFY, buyable=False,
                    unit="result", price=100, start_fee=0, fit=10, coverage=10, success_rate=1.0, rating=5.0,
                    users_30d=1000)  # $0.005 for 50 results, 100 points
    result = pick(CHEAP, actor)
    assert result.best is not None and result.best.offer.key == actor.key
    assert result.best_buyable is not None and result.best_buyable.offer.key == CHEAP.key


def test_the_overall_best_never_contradicts_the_best_buyable():
    a = offer("http://a.test/x", 50_000, fit=5, coverage=5)  # 55
    actor = replace(CHEAP, key="apify/x", verdict=pricing.APIFY, buyable=False, unit="result", price=1_600,
                    fit=7, coverage=7, users_30d=1000)  # $0.08 for 50 results: 28 + 21 + 10 + 10 = 69
    b = offer("http://b.test/x", 100_000, fit=7, coverage=6)  # 28 + 18 + 20 = 66
    c = offer("http://c.test/x", 200_000, fit=10, coverage=10)  # 90
    result = pick(a, actor, b, c)
    assert result.best_buyable is not None
    assert result.best is not None and result.best.offer.key in (result.best_buyable.offer.key, actor.key)


def test_a_free_url_that_fits_is_the_cheapest_and_buyable():
    free = replace(offer("http://a.test/free", 0), verdict=pricing.FREE, price=None)
    result = pick(free, offer("http://b.test/x", 50_000))
    assert result.cheapest_buyable is not None and result.cheapest_buyable.offer.key == free.key
    assert result.cheapest_buyable.cost == 0


def test_a_duplicate_keeps_the_copy_with_a_live_price():
    listed = replace(CHEAP, price=10_000, live=False)
    result = pick(listed, CHEAP)
    assert len(result.scored) == 1 and result.scored[0].cost == CHEAP.price


def test_ties_on_price_go_to_the_better_offer():
    worse = offer("http://b.test/x", 50_000, fit=7, coverage=7)
    result = pick(worse, CHEAP)
    assert result.best_buyable is not None and result.best_buyable.offer.key == CHEAP.key


def test_nothing_to_recommend_says_so():
    assert pricing.explain(pick()) == "Nothing Sentinel can buy fits this task."
