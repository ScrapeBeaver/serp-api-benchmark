"""The pricing models found in the field survey (research/pricing): each mechanic once, on a
contract small enough to check by hand."""

from __future__ import annotations

import copy
import datetime as dt

import pytest
from pydantic import ValidationError

from serp_bench.contract import ProviderConfig
from serp_bench.pricing import (
    HEADLINE_VOLUMES,
    LADDER_VOLUMES,
    Shape,
    Unknown,
    plan_edges,
    price_curve,
    price_per_1k,
    units_for,
    volume_grid,
)

BASE = {
    "schema_version": 1,
    "id": "example",
    "name": "Example",
    "product": {"name": "Search", "endpoint": "GET https://api.example.com", "mode": "light"},
    "billing": [
        {
            "effective_from": "2026-01-01",
            "checked_at": "2026-01-01",
            "sources": ["https://example.com/pricing"],
            "meter": {"unit": "credit"},
            "request_cost": {"base": 1},
            "purchase": {
                "model": "subscription",
                "pricing": "plan_units",
                "plans": [
                    {"id": "s", "name": "S", "price": 50, "period": "month", "units": 50_000},
                    {"id": "l", "name": "L", "price": 300, "period": "month", "units": 1_000_000},
                ],
                "on_limit": "block",
            },
        }
    ],
}


def billing(**changes):
    """BASE with parts of its billing entry replaced."""
    data = copy.deepcopy(BASE)
    data["billing"][0].update(changes)
    return ProviderConfig.model_validate(data).billing[0]


def per_1k(entry, volume, shape=None, beyond_plan=True):
    quote = price_per_1k(shape or Shape(), volume, entry, beyond_plan=beyond_plan)
    assert not isinstance(quote, Unknown), quote
    return quote


# --- prepaid packs with an expiry (Serper-like) ---------------------------------------------

PACKS = {
    "model": "prepaid_packs",
    "pricing": "plan_units",
    "plans": [
        {
            "id": "p50k",
            "name": "50k",
            "price": 50,
            "period": "one_time",
            "units": 50_000,
            "unused_units": {"expire_after_days": 180},
        },
        {
            "id": "p500k",
            "name": "500k",
            "price": 375,
            "period": "one_time",
            "units": 500_000,
            "unused_units": {"expire_after_days": 180},
        },
    ],
    "on_limit": "block",
}


def test_a_pack_is_spread_over_the_months_it_lasts():
    # 50,000 credits at 10,000 a month last 5 months, inside the 180 days: $10 a month.
    quote = per_1k(billing(purchase=PACKS), 10_000)
    assert quote.plan_id == "p50k"
    assert quote.monthly_cost == pytest.approx(10.0)
    assert quote.price_per_1k == pytest.approx(1.00)


def test_a_pack_that_outlives_its_expiry_is_not_fully_used():
    # At 1,000 a month the 50k pack would last 50 months, but it expires after 180 days
    # (5.91 months): the whole $50 buys 5.91 months.
    quote = per_1k(billing(purchase=PACKS), 1_000)
    months = 180 / (365.25 / 12)
    assert quote.monthly_cost == pytest.approx(50 / months)
    assert "only" in quote.formula


def test_packs_can_be_bought_more_than_once_a_month():
    # 2,000,000 a month is four 500k packs: the unit price of the largest pack.
    quote = per_1k(billing(purchase=PACKS), 2_000_000)
    assert quote.plan_id == "p500k"
    assert quote.price_per_1k == pytest.approx(0.75)


def test_a_pack_with_no_stated_expiry_counts_for_one_month():
    packs = copy.deepcopy(PACKS)
    for plan in packs["plans"]:
        plan["unused_units"] = "unknown"
    quote = per_1k(billing(purchase=packs), 10_000)
    assert quote.monthly_cost == pytest.approx(50.0)  # the smallest pack, used for a month


def test_a_never_expiring_pack_is_spread_over_at_most_a_year():
    packs = copy.deepcopy(PACKS)
    for plan in packs["plans"]:
        plan["unused_units"] = "never_expire"
    quote = per_1k(billing(purchase=packs), 1_000)  # 50 months of supply
    assert quote.monthly_cost == pytest.approx(50 / 12)


# --- what happens at the limit ---------------------------------------------------------------


def purchase_with(**changes):
    purchase = copy.deepcopy(BASE["billing"][0]["purchase"])
    purchase.update(changes)
    return purchase


def test_beyond_the_largest_plan_is_unknown_when_requests_are_blocked():
    quote = price_per_1k(Shape(), 2_000_000, billing())
    assert isinstance(quote, Unknown)
    assert "no published plan" in quote.reason


def test_auto_renew_buys_the_plan_again():
    entry = billing(purchase=purchase_with(on_limit="auto_renew"))
    # Counting whole plans only, nothing includes the volume.
    assert isinstance(price_per_1k(Shape(), 2_500_000, entry, beyond_plan=False), Unknown)
    quote = per_1k(entry, 2_500_000)
    assert (quote.plan_price, quote.extra_cost, quote.extra) == (300, 600, "bought 3 times")
    assert quote.plan_id == "l"
    assert quote.monthly_cost == pytest.approx(900)  # three times the 1M plan
    assert "3 times" in quote.formula


def test_each_plan_can_have_its_own_overage_rate():
    purchase = purchase_with(on_limit="overage")
    purchase["plans"][0]["overage_unit_price"] = 0.004  # four times the in-plan rate
    purchase["plans"][1]["overage_unit_price"] = 0.0006
    entry = billing(purchase=purchase)
    # 60,000: S + 10,000 over at $0.004 = $90, cheaper than L at $300.
    quote = per_1k(entry, 60_000)
    assert (quote.plan_id, quote.monthly_cost) == ("s", pytest.approx(90))
    assert (quote.plan_price, quote.extra_cost) == (50, pytest.approx(40))
    assert quote.extra == "10,000 credits over the plan"
    # 200,000: S + 150,000 over would be $650, so the L plan in full is cheaper.
    quote = per_1k(entry, 200_000)
    assert (quote.plan_id, quote.monthly_cost, quote.extra) == ("l", pytest.approx(300), None)
    # Counting whole plans only, 60,000 already needs the L plan.
    quote = per_1k(entry, 60_000, beyond_plan=False)
    assert (quote.plan_id, quote.monthly_cost) == ("l", pytest.approx(300))
    # 2,000,000: L + 1,000,000 over at $0.0006 = $900.
    quote = per_1k(entry, 2_000_000)
    assert (quote.plan_id, quote.monthly_cost) == ("l", pytest.approx(900))


def test_overage_at_the_plans_own_rate():
    entry = billing(purchase=purchase_with(on_limit="overage", overage={"rule": "plan_rate"}))
    quote = per_1k(entry, 2_000_000)
    assert quote.price_per_1k == pytest.approx(0.30)


# --- surcharges ------------------------------------------------------------------------------


def test_multiply_then_add():
    # DataForSEO-like: cost = base x multipliers + additions.
    cost = {
        "base": 2,
        "rules": [
            {"when": {"query_operators": True}, "multiply": 5},
            {"when": {"ai_overview": True}, "add": 2},
            {"when": {"device": "mobile"}, "multiply": 1},
        ],
        "stacking": "multiply_then_add",
    }
    entry = billing(request_cost=cost)
    assert units_for(Shape(query_operators=True, ai_overview=True), entry) == 12
    assert units_for(Shape(device="mobile", ai_overview=True), entry) == 4


def test_a_surcharge_charged_only_when_returned_is_an_upper_bound():
    cost = {
        "base": 1,
        "rules": [{"when": {"ai_overview": True}, "multiply": 3, "applies": "if_returned"}],
        "stacking": "first_match",
    }
    quote = per_1k(billing(request_cost=cost), 10_000, Shape(ai_overview=True))
    assert quote.units_per_request == 3
    assert any("upper bound" in note for note in quote.notes)
    assert not per_1k(billing(request_cost=cost), 10_000).notes


# --- a balance in money ----------------------------------------------------------------------


def payg(tiers, unused="never_expire"):
    return billing(
        meter={"unit": "usd"},
        request_cost={"base": 0.001},
        purchase={
            "model": "pay_as_you_go",
            "pricing": "pay_as_you_go",
            "tiers": tiers,
            "unused_units": unused,
        },
    )


def test_a_monthly_commitment_is_paid_in_full():
    # Scrapeless-like: $49 a month buys a 10% lower rate.
    tiers = [{"from_amount": 49, "basis": "monthly_commitment", "rate_multiplier": 0.9}]
    entry = payg(tiers)
    assert per_1k(entry, 10_000).monthly_cost == pytest.approx(10.0)  # list rate, no commitment
    quote = per_1k(entry, 50_000)  # list $50; committed: max($49, $45) = $49
    assert quote.monthly_cost == pytest.approx(49.0)
    assert per_1k(entry, 1_000_000).price_per_1k == pytest.approx(0.90)


def test_a_usage_tier_applies_only_once_reached():
    tiers = [{"from_amount": 500, "basis": "monthly_usage", "unit_price": 0.0007}]
    entry = payg(tiers)
    assert per_1k(entry, 100_000).price_per_1k == pytest.approx(1.00)
    assert per_1k(entry, 1_000_000).price_per_1k == pytest.approx(0.70)


def test_a_tier_needs_exactly_one_rate():
    with pytest.raises(ValidationError, match="exactly one"):
        payg([{"from_amount": 10}])
    with pytest.raises(ValidationError, match="exactly one"):
        payg([{"from_amount": 10, "rate_multiplier": 0.5, "unit_price": 0.001}])


# --- a rate card that continues beyond the plan ---------------------------------------------


def test_a_rate_card_plan_can_be_exceeded_at_its_own_rates():
    purchase = {
        "model": "subscription",
        "pricing": "rate_card",
        "shapes": {"std": {"when": {"option": "standard"}}},
        "benchmarked_shape": "std",
        "plans": [
            {"id": "a", "name": "A", "price": 50, "period": "month", "rates_per_1k": {"std": 1.0}},
            {"id": "b", "name": "B", "price": 500, "period": "month", "rates_per_1k": {"std": 0.5}},
        ],
    }
    blocked = billing(meter={"unit": "request"}, purchase=purchase)
    assert isinstance(price_per_1k(Shape(), 2_000_000, blocked), Unknown)
    purchase |= {"on_limit": "overage", "overage": {"rule": "plan_rate"}}
    open_ended = billing(meter={"unit": "request"}, purchase=purchase)
    assert isinstance(price_per_1k(Shape(), 2_000_000, open_ended, beyond_plan=False), Unknown)
    quote = per_1k(open_ended, 2_000_000)
    assert (quote.plan_price, quote.extra_cost) == (500, pytest.approx(500))
    assert (quote.plan_id, quote.price_per_1k) == ("b", pytest.approx(0.50))


# --- the curve -------------------------------------------------------------------------------


def test_the_grid_is_six_points_a_decade():
    grid = volume_grid(1_000, 100_000)
    assert grid == [1_000, 1_500, 2_000, 3_000, 5_000, 7_500, 10_000, 15_000, 20_000, 30_000,
                    50_000, 75_000, 100_000]  # fmt: skip


def test_the_curve_includes_the_headline_volumes_and_the_plan_edges():
    entry = billing()
    assert plan_edges(Shape(), entry) == [50_000, 50_001, 1_000_000, 1_000_001]
    curve = {point.monthly_volume: point.quote for point in price_curve(Shape(), entry)}
    assert set(HEADLINE_VOLUMES) <= set(curve)
    assert set(LADDER_VOLUMES) <= set(curve)  # the price table reads exact points, not a guess
    assert set(HEADLINE_VOLUMES) <= set(LADDER_VOLUMES)
    assert curve[50_000].price_per_1k == pytest.approx(1.00)  # the S plan, full
    assert curve[50_001].plan_id == "l"  # one request more needs the L plan
    assert curve[50_001].price_per_1k == pytest.approx(300 / 50_001 * 1000)
    assert isinstance(curve[10_000_000], Unknown)  # beyond the published plans


def test_the_price_per_1k_never_rises_with_volume_inside_a_plan():
    entry = billing()
    previous = None
    for volume in (1_000, 5_000, 20_000, 50_000):
        price = per_1k(entry, volume).price_per_1k
        assert previous is None or price < previous
        previous = price


# --- terms and announced prices --------------------------------------------------------------


def config(**top):
    data = copy.deepcopy(BASE)
    data.update(top)
    return ProviderConfig.model_validate(data)


def test_a_prohibition_on_benchmarking_keeps_the_provider_in_draft():
    terms = {"benchmarking": "prohibited", "url": "https://example.com/terms", "quote": "..."}
    assert config(terms=terms).status == "draft"
    with pytest.raises(ValidationError, match="stays in `draft`"):
        config(terms=terms, status="shadow")
    allowed = terms | {"permission": "email from the provider, 2026-11-12"}
    assert config(terms=allowed, status="shadow").status == "shadow"


def test_an_announced_price_is_not_current_until_its_day():
    data = copy.deepcopy(BASE)
    future = copy.deepcopy(data["billing"][0])
    future["effective_from"] = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)).isoformat()
    future["changes"] = [
        {
            "what": "S plan, price",
            "was": "$50",
            "now": "$60",
            "source": "https://example.com/pricing",
        }
    ]
    data["billing"].append(future)
    parsed = ProviderConfig.model_validate(data)
    assert parsed.current_billing is parsed.billing[0]


def test_early_renewal_at_a_threshold_costs_one_plan_more_near_the_edge():
    # ScrapingBee-like: the plan renews at 98% used. Ten full plans of use trigger an
    # eleventh purchase; 9.9 plans do not.
    entry = billing(purchase=purchase_with(on_limit="auto_renew", auto_renew_at=0.98))
    assert per_1k(entry, 10_000_000).monthly_cost == pytest.approx(11 * 300)
    assert per_1k(entry, 9_900_000).monthly_cost == pytest.approx(10 * 300)
    assert per_1k(entry, 1_000_000).monthly_cost == pytest.approx(300)  # fits one plan: no renewal


def test_further_pages_inside_one_request_are_each_charged():
    # cloro-like: +2 credits for every page after the first, all in one request.
    cost = {
        "base": 5,
        "rules": [{"when": {"page": "2+"}, "add": 2}],
        "stacking": "sum",
        "per_page": "included",
    }
    entry = billing(request_cost=cost)
    assert units_for(Shape(page=2), entry) == 7
    assert units_for(Shape(page=3), entry) == 9
    separate = billing(request_cost=cost | {"per_page": "separate_request"})
    assert units_for(Shape(page=3), separate) == 7


def test_a_free_tier_of_unstated_size():
    purchase = purchase_with(free={"units": "unknown", "period": "once"})
    assert billing(purchase=purchase).purchase.free.units == "unknown"


def test_a_later_entry_must_say_what_changed():
    data = copy.deepcopy(BASE)
    later = copy.deepcopy(data["billing"][0]) | {"effective_from": "2026-06-01"}
    data["billing"].append(later)
    with pytest.raises(ValidationError, match="needs `changes`"):
        ProviderConfig.model_validate(data)
    later["changes"] = [
        {
            "what": "S plan, monthly price",
            "was": "$50",
            "now": "$60",
            "source": "https://example.com/pricing",
            "quote": "Starter: $60 / month",
        }
    ]
    later["review"] = {"confirmed_by": "a maintainer", "confirmed_at": "2026-06-01"}
    parsed = ProviderConfig.model_validate(data)
    assert parsed.billing[1].review.confirmed_by == "a maintainer"


def test_counting_whole_plans_only_ignores_what_happens_at_the_limit():
    for on_limit in ("block", "overage", "auto_renew", "unknown"):
        purchase = purchase_with(on_limit=on_limit, overage={"rule": "next_plan_rate"})
        quote = per_1k(billing(purchase=purchase), 60_000, beyond_plan=False)
        assert (quote.plan_id, quote.monthly_cost) == ("l", pytest.approx(300)), on_limit
        assert not quote.notes and quote.extra_cost == 0


def test_a_provider_that_refuses_requests_at_the_limit_needs_the_next_plan():
    for on_limit in ("block", "unknown"):
        quote = per_1k(billing(purchase=purchase_with(on_limit=on_limit)), 60_000)
        assert (quote.plan_id, quote.plan_price, quote.extra_cost) == ("l", 300, 0), on_limit
