"""units_for, price_per_1k and billing_at over synthetic contracts and the real one."""

from __future__ import annotations

import datetime as dt

import pytest

from serp_bench.pricing import (
    MixItem,
    Shape,
    Unknown,
    billing_at,
    cheapest_for_mix,
    price_per_1k,
    shape_for,
    units_for,
)


def entry(config, day="2026-03-01"):
    found = billing_at(config, dt.date.fromisoformat(day))
    assert not isinstance(found, Unknown)
    return found


# --- billing_at ---------------------------------------------------------------------------


def test_billing_at_returns_the_entry_in_force(credits):
    first, second = credits.billing
    assert billing_at(credits, dt.date(2026, 1, 1)) is first
    assert billing_at(credits, dt.date(2026, 5, 31)) is first
    assert billing_at(credits, dt.date(2026, 6, 1)) is second
    assert billing_at(credits, dt.date(2030, 1, 1)) is second


def test_billing_at_before_the_first_entry_is_unknown(credits):
    result = billing_at(credits, dt.date(2025, 12, 31))
    assert isinstance(result, Unknown)
    assert "2026-01-01" in result.reason


# --- units per request --------------------------------------------------------------------


def test_base_shape_costs_the_base(credits):
    assert units_for(Shape(), entry(credits)) == 5


@pytest.mark.parametrize(
    ("shape", "units"),
    [
        (Shape(mode="full"), 10),
        (Shape(device="mobile"), 10),
        (Shape(query_operators=True), 25),
        (Shape(page=2), 5),  # a separate request at the base cost
    ],
)
def test_a_single_matching_rule_is_applied(credits, shape, units):
    assert units_for(shape, entry(credits)) == units


def test_an_option_rule_never_matches(credits):
    # `deep_scrape` is a provider option the benchmark never sends.
    assert units_for(Shape(), entry(credits)) == 5


def test_an_undocumented_key_is_unknown(credits):
    result = units_for(Shape(location="city"), entry(credits))
    assert isinstance(result, Unknown)
    assert "location" in result.reason
    assert isinstance(units_for(Shape(ai_overview=True), entry(credits)), Unknown)


def test_several_rules_with_unknown_stacking_is_unknown(credits):
    result = units_for(Shape(device="mobile", query_operators=True), entry(credits))
    assert isinstance(result, Unknown)
    assert "combine" in result.reason


@pytest.mark.parametrize(
    ("stacking", "units"),
    [("first_match", 10), ("max", 25), ("sum", 30), ("product", 50)],
)
def test_stacking_modes(credits, stacking, units):
    billing = entry(credits, "2026-07-01")
    cost = billing.request_cost.model_copy(update={"stacking": stacking})
    billing = billing.model_copy(update={"request_cost": cost})
    assert units_for(Shape(device="mobile", query_operators=True), billing) == units


def test_unknown_base_is_unknown(credits):
    billing = entry(credits)
    cost = billing.request_cost.model_copy(update={"base": "unknown"})
    result = units_for(Shape(), billing.model_copy(update={"request_cost": cost}))
    assert isinstance(result, Unknown)


def test_page_two_is_unknown_when_per_page_is_not_documented(credits):
    billing = entry(credits)
    cost = billing.request_cost.model_copy(update={"per_page": "unknown"})
    billing = billing.model_copy(update={"request_cost": cost})
    assert isinstance(units_for(Shape(page=2), billing), Unknown)
    assert units_for(Shape(), billing) == 5


def test_shape_for_normalises_the_benchmarked_mode(credits):
    assert shape_for(credits, mode="light").mode is None
    assert shape_for(credits, mode="full").mode == "full"


# --- price per 1,000: plans that buy units --------------------------------------------------


@pytest.mark.parametrize(
    ("volume", "plan", "per_1k"),
    [(10_000, "lite", 4.00), (100_000, "standard", 0.90), (600_000, "pro", 200 / 600)],
)
def test_cheapest_plan_that_covers_the_volume(credits, volume, plan, per_1k):
    quote = price_per_1k(Shape(), volume, entry(credits))
    assert quote.plan_id == plan
    assert quote.price_per_1k == pytest.approx(per_1k)
    assert quote.units_per_request == 5
    assert quote.rules_applied == 0
    assert str(volume // 1000) in quote.formula.replace(",", "")


def test_a_rule_changes_the_plan_needed(credits):
    quote = price_per_1k(Shape(device="mobile"), 30_000, entry(credits))
    assert (quote.plan_id, quote.rules_applied) == ("standard", 1)  # 300,000 credits
    assert quote.price_per_1k == pytest.approx(3.00)


def test_volume_beyond_the_largest_plan_is_unknown_when_the_limit_blocks(credits):
    result = price_per_1k(Shape(), 1_000_000, entry(credits))
    assert isinstance(result, Unknown)
    assert "no published plan" in result.reason


def test_overage_at_a_unit_price(credits):
    # 250,000 credits: Lite $45 + 50,000 x $0.0003 = $60, cheaper than Standard at $90.
    quote = price_per_1k(Shape(), 50_000, entry(credits, "2026-07-01"))
    assert quote.plan_id == "lite"
    assert quote.monthly_cost == pytest.approx(60)
    assert quote.price_per_1k == pytest.approx(1.20)


def test_the_price_in_force_on_each_day_differs(credits):
    before = price_per_1k(Shape(), 10_000, entry(credits, "2026-05-31"))
    after = price_per_1k(Shape(), 10_000, entry(credits, "2026-06-01"))
    assert (before.price_per_1k, after.price_per_1k) == (pytest.approx(4.0), pytest.approx(4.5))


def test_unknown_units_make_the_price_unknown(credits):
    assert isinstance(price_per_1k(Shape(location="city"), 10_000, entry(credits)), Unknown)


def test_volume_must_be_positive(credits):
    with pytest.raises(ValueError):
        price_per_1k(Shape(), 0, entry(credits))


# --- price per 1,000: the real ScrapeBeaver declaration -------------------------------------


@pytest.mark.parametrize(
    ("volume", "plan", "per_1k"),
    [
        (60, "free", 0.0),
        (150, "starter", 25 / 150 * 1000),  # the free plan blocks at its limit
        (10_000, "starter", 2.50),
        (50_000, "starter", 0.90),  # $25 + 25,000 at the Growth rate
        (250_000, "growth", 0.68),  # $80 + 150,000 at the Business rate
        (100_000, "growth", 0.80),
        (1_000_000, "business", 0.60),
    ],
)
def test_scrapebeaver_quotes(specs, volume, plan, per_1k):
    billing = specs["scrapebeaver"].config.current_billing
    quote = price_per_1k(Shape(), volume, billing)
    assert quote.plan_id == plan
    assert quote.price_per_1k == pytest.approx(per_1k)


def test_scrapebeaver_has_one_price_for_every_shape(specs):
    billing = specs["scrapebeaver"].config.current_billing
    shape = Shape(device="mobile", location="city", ai_overview=True, query_operators=True)
    assert units_for(shape, billing) == 1


def test_no_next_plan_means_no_overage_rate(specs):
    billing = specs["scrapebeaver"].config.current_billing
    assert isinstance(price_per_1k(Shape(), 10_000_000, billing), Unknown)


# --- price per 1,000: rate cards -----------------------------------------------------------


@pytest.mark.parametrize(
    ("volume", "plan", "per_1k"),
    [(10_000, "p19", 1.90), (100_000, "p99", 0.99), (1_000_000, "p999", 0.999)],
)
def test_rate_card_base_shape(rates, volume, plan, per_1k):
    quote = price_per_1k(Shape(), volume, entry(rates))
    assert quote.plan_id == plan
    assert quote.price_per_1k == pytest.approx(per_1k)
    assert "premium" in quote.notes[0]


def test_rate_card_named_shape_for_another_mode(rates):
    quote = price_per_1k(Shape(mode="full"), 10_000, entry(rates))
    assert quote.plan_id == "p19"  # 10,000 x $1.50 / 1,000 = $15 of a $19 plan
    assert "premium_js" in quote.notes[0]
    quote = price_per_1k(Shape(mode="full"), 20_000, entry(rates))
    assert quote.plan_id == "p99"  # $30 does not fit in $19


def test_rate_card_without_a_price_for_the_shape_is_unknown(rates):
    result = price_per_1k(Shape(device="mobile"), 10_000, entry(rates))
    assert isinstance(result, Unknown)
    assert "device" in result.reason


def test_rate_card_volume_beyond_every_plan_is_unknown(rates):
    assert isinstance(price_per_1k(Shape(), 10_000_000, entry(rates)), Unknown)


# --- price per 1,000: pay as you go ---------------------------------------------------------


@pytest.mark.parametrize(
    ("volume", "per_1k"),
    [
        # list rate $2.00. A $100 deposit halves it and, never expiring, is spread over at
        # most 12 months ($8.33 a month), so it already pays off at 10,000 a month.
        (10_000, 1.00),
        # the $1,000 deposit (x0.25) costs at least $83.33 a month: cheaper than $100 at x0.5
        (100_000, 0.8333333),
        (1_000_000, 0.50),
    ],
)
def test_pay_as_you_go_tiers(payg, volume, per_1k):
    quote = price_per_1k(Shape(), volume, entry(payg))
    assert quote.plan_id is None
    assert quote.price_per_1k == pytest.approx(per_1k)


def test_pay_as_you_go_notes_the_minimum_topup(payg):
    quote = price_per_1k(Shape(), 10_000, entry(payg))  # $20 a month, top-up from $50
    assert any("top-up" in note for note in quote.notes)


def test_pay_as_you_go_multiplier_and_product_stacking(payg):
    billing = entry(payg)
    assert units_for(Shape(query_operators=True), billing) == pytest.approx(0.01)
    assert units_for(Shape(query_operators=True, page=3), billing) == pytest.approx(0.01)
    quote = price_per_1k(Shape(query_operators=True), 10_000, billing)
    assert quote.price_per_1k == pytest.approx(5.00)  # $100 at list reaches the first tier
    assert quote.rules_applied == 1


# --- a month of mixed requests: the cheapest plan that covers all of it ----------------------


def mix(*parts):
    return [MixItem(label, shape, volume) for label, shape, volume in parts]


def test_a_credit_plan_must_cover_the_sum_of_every_shape(credits):
    """30,000 desktop at 5 credits and 6,000 mobile at 10: 210,000 credits. The 200,000-credit
    plan is 10,000 short, so the next one is needed, although neither part alone needs it."""
    billing = entry(credits)
    desktop, mobile = Shape(), Shape(device="mobile")
    quote = cheapest_for_mix(mix(("core", desktop, 30_000), ("mobile", mobile, 6_000)), billing)
    assert quote.used == 210_000 and quote.plan_id == "standard" and quote.monthly_cost == 90
    assert quote.allowance == 1_000_000
    assert "30,000 x 5 = 150,000" in quote.lines[0] and "6,000 x 10 = 60,000" in quote.lines[1]

    smaller = cheapest_for_mix(mix(("core", desktop, 28_000), ("mobile", mobile, 6_000)), billing)
    assert smaller.used == 200_000 and smaller.plan_id == "lite" and smaller.monthly_cost == 40


def test_a_mix_with_an_unpriced_shape_is_unknown_and_says_which(credits):
    billing = entry(credits)
    result = cheapest_for_mix(
        mix(("core", Shape(), 1_000), ("city", Shape(location="city"), 10)), billing
    )
    assert isinstance(result, Unknown) and result.reason.startswith("city:")


def test_a_mix_no_plan_covers_is_unknown(credits):
    result = cheapest_for_mix(mix(("core", Shape(), 5_000_000)), entry(credits))
    assert isinstance(result, Unknown) and "no published plan" in result.reason


def test_a_rate_card_adds_money_not_units(rates):
    """15,000 premium at $1.00 and 2,000 premium+JS at $1.50 per 1,000 on the $19 plan is
    $18 of $19. One more thousand premium no longer fits, and the $99 plan prices the same
    requests at its own, lower rates."""
    billing = entry(rates)
    premium, premium_js = Shape(), Shape(mode="full")
    quote = cheapest_for_mix(mix(("core", premium, 15_000), ("full", premium_js, 2_000)), billing)
    assert quote.plan_id == "p19" and quote.used == pytest.approx(18.0) and quote.allowance == 19
    assert quote.unit_label == "USD"

    larger = cheapest_for_mix(mix(("core", premium, 17_000), ("full", premium_js, 2_000)), billing)
    assert larger.plan_id == "p99" and larger.monthly_cost == 99
    assert larger.used == pytest.approx(17_000 * 0.70 / 1000 + 2_000 * 1.10 / 1000)


def test_pay_as_you_go_is_the_bill_itself(payg):
    quote = cheapest_for_mix(
        mix(("core", Shape(), 10_000), ("ops", Shape(query_operators=True), 1_000)), entry(payg)
    )
    assert quote.plan_id is None and quote.allowance is None
    assert quote.monthly_cost == pytest.approx(10_000 * 0.002 + 1_000 * 0.010)


def test_empty_items_are_ignored_and_nothing_at_all_is_an_error(credits):
    billing = entry(credits)
    quote = cheapest_for_mix(mix(("core", Shape(), 100), ("none", Shape(), 0)), billing)
    assert len(quote.lines) == 1
    with pytest.raises(ValueError):
        cheapest_for_mix([], billing)
