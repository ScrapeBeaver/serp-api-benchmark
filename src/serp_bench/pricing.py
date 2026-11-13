"""What a request costs, computed from what a provider declares in `provider.yaml`.

Pure functions over the contract. When the declaration does not answer a question, the
answer is `Unknown` with the reason, never a guess: an unknown is something to measure.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Literal

from serp_bench.contract import (
    BillingEntry,
    CostRule,
    ExpireAfter,
    OverageRule,
    OverageUnitPrice,
    PayAsYouGoPurchase,
    PlanUnitsPurchase,
    ProviderConfig,
    RateCardPurchase,
    UnitPlan,
    UnusedUnits,
    When,
)

# A one-off purchase (a prepaid pack, a deposit) is spread over the months it lasts, up to
# its expiry and never over more than this many months. Without the cap a buyer of 10,000
# searches a month would be quoted the unit price of a pack that lasts a century.
AMORTISATION_MONTHS = 12
DAYS_PER_MONTH = 365.25 / 12

# The monthly volumes every provider is quoted at.
HEADLINE_VOLUMES: tuple[int, ...] = (10_000, 100_000, 1_000_000, 10_000_000)
# The full ladder for the price table: every step from the smallest paid plans on the market
# to the largest published ones. A provider with no public price at a step shows as such.
LADDER_VOLUMES: tuple[int, ...] = (
    10_000, 25_000, 50_000, 100_000, 250_000, 500_000,
    1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000,
)  # fmt: skip


@dataclass(frozen=True)
class Unknown:
    """The declaration does not say. `reason` names what is missing."""

    reason: str


@dataclass(frozen=True)
class Shape:
    """A request as billing sees it. The defaults are the base shape: the benchmarked mode,
    desktop, page 1, country-level location, no search operators, no AI Overview."""

    mode: Literal["light", "full"] | None = None  # None: the benchmarked mode
    device: Literal["desktop", "mobile"] = "desktop"
    page: int = 1
    location: Literal["country", "city"] = "country"
    query_operators: bool = False
    ai_overview: bool = False

    def non_base_keys(self) -> set[str]:
        """The keys on which this shape differs from the base shape."""
        keys = set()
        if self.mode is not None:
            keys.add("mode")
        if self.device != "desktop":
            keys.add("device")
        if self.page != 1:
            keys.add("page")
        if self.location != "country":
            keys.add("location")
        if self.query_operators:
            keys.add("query_operators")
        if self.ai_overview:
            keys.add("ai_overview")
        return keys


def shape_for(config: ProviderConfig, **kwargs: object) -> Shape:
    """A Shape for this provider. Asking for the provider's benchmarked mode by name is the
    base shape, so it is normalised to `mode=None`."""
    if kwargs.get("mode") == config.product.mode:
        kwargs["mode"] = None
    return Shape(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class UnitCost:
    units: float
    rules: tuple[CostRule, ...] = ()

    @property
    def upper_bound(self) -> bool:
        """True when a rule that was applied is charged only if the feature is returned."""
        return any(rule.applies != "always" for rule in self.rules)


@dataclass(frozen=True)
class PriceQuote:
    """The price of 1,000 requests of one shape at a monthly volume, and how it was reached."""

    price_per_1k: float
    monthly_cost: float
    monthly_volume: int
    currency: str
    plan_id: str | None
    plan_name: str | None
    units_per_request: float | None
    rules_applied: int
    formula: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    # The bill, taken apart: the price of the plan, and what is paid on top of it.
    plan_price: float | None = None
    extra_cost: float = 0.0
    extra: str | None = None  # what the extra is, in a few words


def billing_at(config: ProviderConfig, day: dt.date) -> BillingEntry | Unknown:
    """The billing entry in force on `day`."""
    entry = config.billing_at(day)
    if entry is None:
        first = config.billing[0].effective_from
        return Unknown(f"no billing entry covers {day}; the first one starts on {first}")
    return entry


# --- units per request -------------------------------------------------------------------


def _matches(when: When, shape: Shape) -> bool:
    if when.option is not None:
        return False  # a provider option the benchmark never sends
    if when.mode is not None and shape.mode != when.mode:
        return False
    if when.device is not None and shape.device != when.device:
        return False
    if when.page is not None and (shape.page == 1) != (when.page == 1):
        return False
    if when.location is not None and shape.location != when.location:
        return False
    if when.query_operators is not None and shape.query_operators != when.query_operators:
        return False
    return when.ai_overview is None or shape.ai_overview == when.ai_overview


def _addition(rule: CostRule, shape: Shape, per_page: str) -> float:
    """What an `add` rule adds. Where several pages come in one request, a rule for further
    pages is charged for each of them: page 3 is two further pages."""
    assert rule.add is not None
    if per_page == "included" and rule.when.page == "2+":
        return rule.add * (shape.page - 1)
    return rule.add


def _apply(rule: CostRule, base: float, shape: Shape, per_page: str) -> float:
    if rule.set_ is not None:
        return rule.set_
    if rule.add is not None:
        return base + _addition(rule, shape, per_page)
    assert rule.multiply is not None
    return base * rule.multiply


def unit_cost(shape: Shape, billing: BillingEntry) -> UnitCost | Unknown:
    """Units charged for one request of `shape`, with the rules that produced the figure."""
    cost = billing.request_cost
    if cost.base == "unknown":
        return Unknown("the base cost of a request is not documented")
    base = float(cost.base)

    undocumented = sorted(shape.non_base_keys() & set(cost.undocumented))
    if undocumented:
        return Unknown(f"the documentation does not price: {', '.join(undocumented)}")
    if shape.page > 1 and cost.per_page == "unknown":
        return Unknown("it is not documented whether each results page is a separate request")

    matched = [rule for rule in cost.rules if _matches(rule.when, shape)]
    if not matched:
        return UnitCost(base)

    def applied(rule: CostRule) -> float:
        return _apply(rule, base, shape, cost.per_page)

    if len(matched) == 1:
        return UnitCost(applied(matched[0]), (matched[0],))

    match cost.stacking:
        case "unknown":
            return Unknown(f"{len(matched)} rules match and it is not documented how they combine")
        case "first_match":
            return UnitCost(applied(matched[0]), (matched[0],))
        case "max":
            best = max(matched, key=applied)
            return UnitCost(applied(best), (best,))
        case "sum":
            total = base + sum(applied(rule) - base for rule in matched)
            return UnitCost(total, tuple(matched))
        case "product":
            if base == 0:
                return UnitCost(0.0, tuple(matched))
            total = base
            for rule in matched:
                total *= applied(rule) / base
            return UnitCost(total, tuple(matched))
        case "multiply_then_add":
            replacing = [rule for rule in matched if rule.set_ is not None]
            if len(replacing) > 1:
                return Unknown("several `set` rules match and only one can replace the base")
            total = replacing[0].set_ if replacing else base
            assert total is not None
            for rule in matched:
                if rule.multiply is not None:
                    total *= rule.multiply
            for rule in matched:
                if rule.add is not None:
                    total += _addition(rule, shape, cost.per_page)
            return UnitCost(total, tuple(matched))
    raise AssertionError(f"unhandled stacking {cost.stacking!r}")  # pragma: no cover


def units_for(shape: Shape, billing: BillingEntry) -> float | Unknown:
    """Units charged for one request of `shape`."""
    result = unit_cost(shape, billing)
    return result if isinstance(result, Unknown) else result.units


# --- price per 1,000 at a monthly volume --------------------------------------------------


def price_per_1k(
    shape: Shape, monthly_volume: int, billing: BillingEntry, *, beyond_plan: bool = True
) -> PriceQuote | Unknown:
    """What `monthly_volume` requests of `shape` really cost in a month, per 1,000.

    The rule is the same for every provider: **the cheapest published way to cover the
    volume under that provider's own terms, divided by the volume.**

    - A plan that includes the volume. A plan larger than needed is paid in full.
    - Where the provider bills further units on top of a plan, a smaller plan plus that
      overage, when it comes to less than the next plan.
    - Where the provider renews a plan early when it runs out, the plan bought as many
      times as it takes.
    - Where the provider simply refuses requests at the limit, only the next plan up.
    - Where there is no plan at all (a balance charged per request, packs bought as
      needed), what the volume itself costs.

    The quote says which of these it is, and takes the bill apart: the plan's price and
    what is paid on top. `beyond_plan=False` counts whole plans only, to see the difference.
    """
    if monthly_volume <= 0:
        raise ValueError("monthly_volume must be positive")
    purchase = billing.purchase
    if isinstance(purchase, PlanUnitsPurchase):
        return _quote_plan_units(shape, monthly_volume, billing, purchase, beyond_plan)
    if isinstance(purchase, RateCardPurchase):
        return _quote_rate_card(shape, monthly_volume, billing, purchase, beyond_plan)
    return _quote_pay_as_you_go(shape, monthly_volume, billing, purchase)


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _num(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.4g}"


def plural(label: str) -> str:
    return label + ("es" if label.endswith(("s", "x", "ch", "sh")) else "s")


def _per_1k(cost: float, volume: int) -> float:
    return cost / volume * 1000


def horizon_months(unused: UnusedUnits) -> float:
    """The number of months a one-off purchase is spread over."""
    if isinstance(unused, ExpireAfter):
        return min(AMORTISATION_MONTHS, unused.expire_after_days / DAYS_PER_MONTH)
    if unused == "never_expire":
        return float(AMORTISATION_MONTHS)
    return 1.0  # expires with the period, or not documented: it has to be used in a month


def _bound_note(cost: UnitCost) -> tuple[str, ...]:
    if not cost.upper_bound:
        return ()
    return ("includes a surcharge that is charged only when the feature is returned: upper bound",)


@dataclass(frozen=True)
class _Option:
    monthly: float
    plan: UnitPlan | None  # None: bought with no plan
    detail: str  # how the monthly cost was reached, for the formula
    note: str | None = None
    plan_name: str | None = None
    extra_cost: float = 0.0
    extra: str | None = None


def _plan_options(
    plans: list[UnitPlan],
    needed: float,
    purchase: PlanUnitsPurchase,
    units: str,
    beyond_plan: bool,
) -> list[_Option]:
    """Every published way to get `needed` units in a month, one per plan that can."""
    options: list[_Option] = []
    rate = purchase.pay_as_you_go
    if rate is not None:
        options.append(
            _Option(
                needed * rate.unit_price,
                None,
                f"{rate.name}: {_num(needed)} {units} at ${rate.unit_price:.6f} each",
                plan_name=rate.name,
            )
        )
    for index, plan in enumerate(plans):
        size = f"{plan.name}: {_money(plan.price)} for {_num(plan.units)} {units}"

        if plan.period == "one_time":
            if plan.price == 0:
                continue  # a one-off free allowance is not a way to buy a month, every month
            # A pack is bought once and used until it is empty or expires.
            months = horizon_months(plan.unused_units)
            lasts = plan.units / needed
            monthly = plan.price * max(needed / plan.units, 1 / months)
            if lasts > months:
                detail = (
                    f"{size}, bought once; it would last {lasts:.1f} months but only "
                    f"{months:.1f} count, so {_money(plan.price)} / {months:.1f}"
                )
            else:
                detail = (
                    f"{size}, bought once; it lasts {lasts:.2f} months, "
                    f"so {_money(plan.price)} / {lasts:.2f}"
                )
            if lasts < 1:
                life = f"{needed / plan.units:.1f} packs a month"
            else:
                life = f"one pack lasts {min(lasts, months):.1f} months"
            note = "a prepaid pack, spread over its life"
            options.append(_Option(monthly, plan, detail, note, extra=life))
            continue

        if plan.units >= needed:
            options.append(_Option(plan.price, plan, size))
            continue

        if not beyond_plan:
            continue  # the plan does not include the volume: the next one up is needed
        on_limit = plan.on_limit or purchase.on_limit
        extra = needed - plan.units
        if on_limit == "auto_renew":
            # The plan is bought again when `auto_renew_at` of it is used, and what is left
            # carries over. So k purchases serve fewer than (k - 1 + share) plans of units:
            # using the last 2% of a plan that renews at 98% already costs another one.
            share = purchase.auto_renew_at
            ratio = needed / plan.units
            times = math.ceil(ratio) if share == 1 else math.floor(ratio + (1 - share)) + 1
            when = "runs out" if share == 1 else f"is {share:.0%} used"
            options.append(
                _Option(
                    plan.price * times,
                    plan,
                    f"{size}, bought {times} times in the month",
                    f"needs early renewal switched on: bought again each time it {when}",
                    extra_cost=plan.price * (times - 1),
                    extra=f"bought {times} times",
                )
            )
            continue
        if on_limit != "overage":
            continue
        if plan.overage_unit_price is not None:
            rate = plan.overage_unit_price
        elif isinstance(purchase.overage, OverageUnitPrice):
            rate = purchase.overage.unit_price
        elif isinstance(purchase.overage, OverageRule) and purchase.overage.rule == "plan_rate":
            rate = plan.price / plan.units
        elif isinstance(purchase.overage, OverageRule) and index + 1 < len(plans):
            # "Extra units at the next plan's rate" prices the stretch between this plan
            # and the next one. Beyond the next plan's size one is on that plan instead,
            # and beyond the largest plan there is no published rate.
            following = plans[index + 1]
            if needed > following.units:
                continue
            rate = following.price / following.units
        else:
            continue
        options.append(
            _Option(
                plan.price + extra * rate,
                plan,
                f"{size} + {_num(extra)} over the plan at ${rate:.6f} each",
                "uses overage",
                extra_cost=extra * rate,
                extra=f"{_num(extra)} {units} over the plan",
            )
        )
    return options


def _quote_plan_units(
    shape: Shape,
    volume: int,
    billing: BillingEntry,
    purchase: PlanUnitsPurchase,
    beyond_plan: bool = True,
) -> PriceQuote | Unknown:
    cost = unit_cost(shape, billing)
    if isinstance(cost, Unknown):
        return cost
    unit = billing.meter.unit_label or billing.meter.unit
    units = plural(unit)
    needed = volume * cost.units
    plans = sorted(purchase.plans, key=lambda plan: plan.units)

    options = _plan_options(plans, needed, purchase, units, beyond_plan)
    if not options:
        return Unknown(
            f"no published plan covers {_num(needed)} {units} a month "
            f"(the largest has {_num(plans[-1].units)})"
        )
    best = min(options, key=lambda option: option.monthly)
    formula = (
        f"{_num(volume)} requests x {_num(cost.units)} per request = {_num(needed)} {units}; "
        f"{best.detail} = {_money(best.monthly)} a month; "
        f"{_money(best.monthly)} / {_num(volume)} x 1,000 = "
        f"{_money(_per_1k(best.monthly, volume))}"
    )
    return PriceQuote(
        price_per_1k=_per_1k(best.monthly, volume),
        monthly_cost=best.monthly,
        monthly_volume=volume,
        currency=billing.currency,
        plan_id=best.plan.id if best.plan else None,
        plan_name=best.plan.name if best.plan else best.plan_name,
        units_per_request=cost.units,
        rules_applied=len(cost.rules),
        formula=formula,
        notes=((best.note,) if best.note else ()) + _bound_note(cost),
        plan_price=best.plan.price if best.plan else None,
        extra_cost=best.extra_cost,
        extra=best.extra,
    )


def _rate_card_shape(shape: Shape, purchase: RateCardPurchase) -> str | Unknown:
    """The named shape of the rate card that prices `shape`.

    The base shape is the benchmarked one. Any other shape is priced by a named shape that
    uses the same provider option as the benchmarked one and whose `when` accounts for every
    key on which the request differs from the base.
    """
    differing = shape.non_base_keys()
    if not differing:
        return purchase.benchmarked_shape
    option = purchase.shapes[purchase.benchmarked_shape].when.option
    for name, named in purchase.shapes.items():
        when = named.when
        if when.option != option or set(when.shape_keys()) != differing:
            continue
        if _matches(when.model_copy(update={"option": None}), shape):
            return name
    return Unknown(f"the rate card has no price for a request with: {', '.join(sorted(differing))}")


def _quote_rate_card(
    shape: Shape,
    volume: int,
    billing: BillingEntry,
    purchase: RateCardPurchase,
    beyond_plan: bool = True,
) -> PriceQuote | Unknown:
    name = _rate_card_shape(shape, purchase)
    if isinstance(name, Unknown):
        return name
    continues = (
        beyond_plan
        and purchase.on_limit == "overage"
        and isinstance(purchase.overage, OverageRule)
        and purchase.overage.rule == "plan_rate"
    )
    covering = []
    for plan in purchase.plans:
        rate = plan.rates_per_1k.get(name)
        if rate is None:
            continue
        spend = volume * rate / 1000
        if spend <= plan.price:
            covering.append((plan.price, plan, rate, spend))
        elif continues:
            covering.append((spend, plan, rate, spend))
    if not covering:
        return Unknown(f"no published plan covers {_num(volume)} '{name}' requests a month")
    monthly, plan, rate, spend = min(covering, key=lambda option: option[0])
    if spend > plan.price:
        used = f"cost {_money(spend)}, more than the {_money(plan.price)} plan, at the same rate"
    else:
        used = f"use {_money(spend)} of the {_money(plan.price)} plan"
    formula = (
        f"{plan.name}: '{name}' at ${rate:.2f} per 1,000; {_num(volume)} requests {used}; "
        f"{_money(monthly)} / {_num(volume)} x 1,000 = {_money(_per_1k(monthly, volume))}"
    )
    return PriceQuote(
        price_per_1k=_per_1k(monthly, volume),
        monthly_cost=monthly,
        monthly_volume=volume,
        currency=billing.currency,
        plan_id=plan.id,
        plan_name=plan.name,
        units_per_request=None,
        rules_applied=0,
        formula=formula,
        notes=(f"priced as the rate-card shape '{name}'",),
        plan_price=plan.price,
        extra_cost=max(0.0, spend - plan.price),
        extra="spending beyond the plan, at its rates" if spend > plan.price else None,
    )


def _quote_pay_as_you_go(
    shape: Shape, volume: int, billing: BillingEntry, purchase: PayAsYouGoPurchase
) -> PriceQuote | Unknown:
    cost = unit_cost(shape, billing)
    if isinstance(cost, Unknown):
        return cost
    list_spend = volume * cost.units
    months = horizon_months(purchase.unused_units)
    base = billing.request_cost.base
    assert base != "unknown"  # unit_cost returned a figure

    # (monthly cost, text for the formula, note)
    options: list[tuple[float, str, str | None]] = [(list_spend, "", None)]
    for tier in purchase.tiers:
        if tier.rate_multiplier is not None:
            factor = tier.rate_multiplier
        else:
            assert tier.unit_price is not None
            factor = tier.unit_price / float(base) if float(base) else 1.0
        spend = list_spend * factor
        label = tier.name or f"from {_money(tier.from_amount)}"
        if tier.basis == "monthly_usage":
            if list_spend < tier.from_amount:
                continue
            options.append((spend, f" x {factor:g} (usage tier {label})", None))
        elif tier.basis == "monthly_commitment":
            monthly = max(tier.from_amount, spend)
            note = (
                f"the {_money(tier.from_amount)} monthly commitment is paid in full"
                if spend < tier.from_amount
                else None
            )
            options.append((monthly, f" x {factor:g} (commitment tier {label})", note))
        else:
            floor = tier.from_amount / months
            monthly = max(floor, spend)
            note = (
                f"a {_money(tier.from_amount)} deposit, spread over {months:.1f} months"
                if spend < floor
                else None
            )
            options.append((monthly, f" x {factor:g} (deposit tier {label})", note))
    monthly, tier_text, tier_note = min(options, key=lambda option: option[0])

    notes = [tier_note] if tier_note else []
    if purchase.minimum_topup and monthly < purchase.minimum_topup:
        notes.append(f"the smallest top-up is {_money(purchase.minimum_topup)}")
    formula = (
        f"{_num(volume)} requests x ${cost.units:.6f}{tier_text} = {_money(monthly)}; "
        f"{_money(monthly)} / {_num(volume)} x 1,000 = {_money(_per_1k(monthly, volume))}"
    )
    return PriceQuote(
        price_per_1k=_per_1k(monthly, volume),
        monthly_cost=monthly,
        monthly_volume=volume,
        currency=billing.currency,
        plan_id=None,
        plan_name=None,
        units_per_request=cost.units,
        rules_applied=len(cost.rules),
        formula=formula,
        notes=tuple(notes) + _bound_note(cost),
    )


def cost_ratio(shape: Shape, billing: BillingEntry) -> float | Unknown:
    """How many times dearer one request of `shape` is than a base request. On a rate card
    the ratio differs from plan to plan; the smallest plan's is given."""
    purchase = billing.purchase
    if isinstance(purchase, RateCardPurchase):
        name = _rate_card_shape(shape, purchase)
        if isinstance(name, Unknown):
            return name
        base_name = purchase.benchmarked_shape
        for plan in sorted(purchase.plans, key=lambda plan: plan.price):
            base_rate = plan.rates_per_1k.get(base_name)
            if base_rate and name in plan.rates_per_1k:
                return plan.rates_per_1k[name] / base_rate
        return Unknown("no plan prices both shapes")
    cost, base = units_for(shape, billing), units_for(Shape(), billing)
    if isinstance(cost, Unknown):
        return cost
    if isinstance(base, Unknown) or base == 0:
        return Unknown("the base cost of a request is not documented")
    return cost / base


# --- the price at every volume -------------------------------------------------------------


@dataclass(frozen=True)
class CurvePoint:
    monthly_volume: int
    quote: PriceQuote | Unknown


def volume_grid(low: int = 1_000, high: int = 100_000_000) -> list[int]:
    """Monthly volumes from `low` to `high`, six per decade, evenly spaced on a log scale."""
    volumes = []
    decade = 10 ** math.floor(math.log10(low))
    while decade <= high:
        for step in (1, 1.5, 2, 3, 5, 7.5):
            volume = int(step * decade)
            if low <= volume <= high:
                volumes.append(volume)
        decade *= 10
    return volumes


def plan_edges(shape: Shape, billing: BillingEntry) -> list[int]:
    """The volumes at which a plan is exactly full, and one request more. The price per
    1,000 jumps there, so a curve that skips these points draws the jump in the wrong place."""
    purchase = billing.purchase
    edges: list[float] = []
    if isinstance(purchase, PlanUnitsPurchase):
        cost = units_for(shape, billing)
        if isinstance(cost, Unknown) or cost <= 0:
            return []
        edges = [plan.units / cost for plan in purchase.plans if plan.period == "month"]
    elif isinstance(purchase, RateCardPurchase):
        name = _rate_card_shape(shape, purchase)
        if isinstance(name, Unknown):
            return []
        edges = [
            plan.price / plan.rates_per_1k[name] * 1000
            for plan in purchase.plans
            if plan.rates_per_1k.get(name)
        ]
    volumes = set()
    for edge in edges:
        full = math.floor(edge + 1e-9)
        if full >= 1:
            volumes.update((full, full + 1))
    return sorted(volumes)


def price_curve(
    shape: Shape,
    billing: BillingEntry,
    volumes: list[int] | None = None,
    *,
    beyond_plan: bool = True,
) -> list[CurvePoint]:
    """The price per 1,000 at each monthly volume. By default: the log grid, the headline
    volumes, the ladder of the price table and every plan edge inside the grid."""
    if volumes is None:
        grid = volume_grid()
        inside = [v for v in plan_edges(shape, billing) if grid[0] <= v <= grid[-1]]
        volumes = sorted(set(grid) | set(HEADLINE_VOLUMES) | set(LADDER_VOLUMES) | set(inside))
    return [
        CurvePoint(volume, price_per_1k(shape, volume, billing, beyond_plan=beyond_plan))
        for volume in volumes
    ]


# --- the cheapest plan that covers a month of mixed requests -------------------------------


@dataclass(frozen=True)
class MixItem:
    """`monthly_volume` requests of one shape, e.g. one pool of the benchmark."""

    label: str
    shape: Shape
    monthly_volume: int


@dataclass(frozen=True)
class MixQuote:
    """The cheapest published way to buy a month of mixed requests, and how much of it is used."""

    monthly_cost: float
    currency: str
    plan_id: str | None
    plan_name: str | None
    used: float  # units, or money for a rate card and pay-as-you-go
    allowance: float | None  # what the plan includes, in the same terms; None when pay-as-you-go
    unit_label: str
    lines: tuple[str, ...]  # one per item: how its share was computed


def cheapest_for_mix(items: list[MixItem], billing: BillingEntry) -> MixQuote | Unknown:
    """The cheapest published plan that covers every item together.

    Same rule as `price_per_1k`, for a month that mixes shapes: units (or money) are added
    up over the items, and the cheapest plan that includes the total is taken. Overage is
    not used: a budget should hold without it. Unknown as soon as one item cannot be priced
    from the declaration, because a total with a hole in it is not a total.
    """
    items = [item for item in items if item.monthly_volume > 0]
    if not items:
        raise ValueError("nothing to price")
    purchase = billing.purchase
    unit = billing.meter.unit_label or billing.meter.unit

    if isinstance(purchase, RateCardPurchase):
        names = []
        for item in items:
            name = _rate_card_shape(item.shape, purchase)
            if isinstance(name, Unknown):
                return Unknown(f"{item.label}: {name.reason}")
            names.append(name)
        covering = []
        for plan in purchase.plans:
            if any(name not in plan.rates_per_1k for name in names):
                continue
            spends = [
                item.monthly_volume * plan.rates_per_1k[name] / 1000
                for item, name in zip(items, names, strict=True)
            ]
            if sum(spends) <= plan.price:
                covering.append((plan.price, plan, spends))
        if not covering:
            return Unknown("no published plan covers this volume")
        _, plan, spends = min(covering, key=lambda option: option[0])
        lines = tuple(
            f"{item.label}: {_num(item.monthly_volume)} x ${plan.rates_per_1k[name]:.2f} "
            f"per 1,000 ('{name}') = {_money(spend)}"
            for item, name, spend in zip(items, names, spends, strict=True)
        )
        return MixQuote(
            plan.price, billing.currency, plan.id, plan.name, sum(spends), plan.price, "USD", lines
        )

    costs = []
    for item in items:
        cost = units_for(item.shape, billing)
        if isinstance(cost, Unknown):
            return Unknown(f"{item.label}: {cost.reason}")
        costs.append(cost)
    totals = [item.monthly_volume * cost for item, cost in zip(items, costs, strict=True)]
    total = sum(totals)

    if isinstance(purchase, PlanUnitsPurchase):
        lines = tuple(
            f"{item.label}: {_num(item.monthly_volume)} x {cost:g} = "
            f"{_num(subtotal)} {plural(unit)}"
            for item, cost, subtotal in zip(items, costs, totals, strict=True)
        )
        covering = [
            plan for plan in purchase.plans if plan.period == "month" and plan.units >= total
        ]
        plan = min(covering, key=lambda option: option.price) if covering else None
        rate = purchase.pay_as_you_go
        if rate is not None and (plan is None or total * rate.unit_price < plan.price):
            cost_no_plan = total * rate.unit_price
            return MixQuote(
                cost_no_plan, billing.currency, None, rate.name, total, None, unit, lines
            )
        if plan is None:
            return Unknown(f"no published plan includes {_num(total)} {plural(unit)} a month")
        return MixQuote(
            plan.price, billing.currency, plan.id, plan.name, total, plan.units, unit, lines
        )

    # pay as you go: the unit is money, so the total is the bill at the list rate
    lines = tuple(
        f"{item.label}: {_num(item.monthly_volume)} x ${cost:.6f} = {_money(subtotal)}"
        for item, cost, subtotal in zip(items, costs, totals, strict=True)
    )
    return MixQuote(total, billing.currency, None, None, total, None, "USD", lines)
