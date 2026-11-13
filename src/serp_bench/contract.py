"""The contract of `provider.yaml`: what a provider is, what it can do and how it bills.

Principles (see providers/README.md for the long form):

1. The file holds what the provider DECLARES in its documentation and on its pricing page.
   What the benchmark MEASURES (the units a response says were charged, for example) lives
   in records, never here.
2. Every fact is a value, `unknown` (not documented; to be measured) or `n/a`. Nothing is
   guessed.
3. Every billing entry names its sources and the date they were read.
4. `billing` is a list ordered by `effective_from`: a price history that is only appended
   to. The last entry is the current one.
5. What one request costs is a function of its shape. The shape keys are a closed
   vocabulary the benchmark understands; a provider option the benchmark never sends is
   written as `option: <name>` and is documentation only.

All models are strict: a key the contract does not define is an error, not a silent no-op.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_SCHEMA_VERSION = 1

Unknown = Literal["unknown"]
NotApplicable = Literal["n/a"]
Tri = bool | Unknown  # true | false | unknown

Mode = Literal["light", "full"]
DeviceName = Literal["desktop", "mobile"]
Location = Literal["country", "city"]
ShapeKey = Literal["mode", "device", "page", "location", "query_operators", "ai_overview"]
SHAPE_KEYS: tuple[str, ...] = (
    "mode",
    "device",
    "page",
    "location",
    "query_operators",
    "ai_overview",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- identity ---------------------------------------------------------------------------


class ProductAlternative(Strict):
    """Another product of the same provider that also returns Google results."""

    name: str
    endpoint: str | None = None
    why_not: str  # why the benchmark does not use this one
    source: str | None = None


class Product(Strict):
    """Exactly what is benchmarked."""

    name: str
    endpoint: str  # method and URL, as documented
    mode: Mode  # the provider's cheapest synchronous Google mode is "light"
    settings: dict[str, Any] = Field(default_factory=dict)  # non-default parameters we send
    # Other endpoints of this provider that return Google results, and why they were not
    # chosen. A reader must be able to see that the cheapest suitable one is benchmarked.
    alternatives: list[ProductAlternative] = Field(default_factory=list)
    notes: str | None = None


class Terms(Strict):
    """What the provider's terms of service say about benchmarking it."""

    url: str | None = None
    checked_at: dt.date | None = None
    benchmarking: Literal["allowed", "not_mentioned", "prohibited", "unknown"] = "unknown"
    quote: str | None = None  # the clause, in the provider's words, kept short
    permission: str | None = None  # written permission that lifts a prohibition: who, when


class Auth(Strict):
    env: list[str] = Field(default_factory=list)  # names of environment variables, never values


class Capabilities(Strict):
    mobile: Tri = "unknown"
    city_location: Tri = "unknown"
    pagination: Tri = "unknown"
    raw_html: Tri = "unknown"
    ai_overview: Tri = "unknown"
    full_mode: Tri = "unknown"  # a dearer mode that returns every block of the page exists


class Adapter(Strict):
    """Technical settings of `adapter.py`. Not facts about the provider."""

    version: str = "1"  # bump when the adapter's reading of responses changes
    base_url: str | None = None
    base_url_env: str | None = None  # an environment variable that overrides base_url
    options: dict[str, Any] = Field(default_factory=dict)


# --- billing: what is counted and what one request costs --------------------------------


class Meter(Strict):
    """What the provider counts."""

    unit: Literal["request", "credit", "usd"]
    unit_label: str | None = None  # the provider's own name for the unit
    shared_across_products: Tri = "unknown"  # one balance for search and other products


class When(Strict):
    """The request shapes a rule applies to. All given keys must match."""

    mode: Mode | None = None
    device: DeviceName | None = None
    page: Literal[1, "2+"] | None = None
    location: Location | None = None
    query_operators: bool | None = None
    ai_overview: bool | None = None
    option: str | None = None  # a provider option the benchmark never sends: documentation

    @model_validator(mode="after")
    def _not_empty(self) -> When:
        if not self.model_fields_set:
            raise ValueError("`when` needs at least one key")
        return self

    def shape_keys(self) -> list[str]:
        return [k for k in SHAPE_KEYS if getattr(self, k) is not None]


class CostRule(Strict):
    """How a request shape changes the cost: exactly one of set, add, multiply."""

    when: When
    set_: float | None = Field(default=None, alias="set", ge=0)
    add: float | None = None
    multiply: float | None = Field(default=None, ge=0)
    # `if_returned`: the surcharge is charged only when the feature is actually in the
    # response (or is charged and then credited back when it is not). The declared price of
    # such a shape is an upper bound.
    applies: Literal["always", "if_returned", "unknown"] = "always"
    source: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _one_operation(self) -> CostRule:
        given = [name for name in ("set_", "add", "multiply") if getattr(self, name) is not None]
        if len(given) != 1:
            raise ValueError("a rule needs exactly one of `set`, `add`, `multiply`")
        return self


class RequestCost(Strict):
    """Units charged for ONE request, by shape."""

    # The benchmarked mode, desktop, page 1, country-level location.
    base: Annotated[float, Field(ge=0)] | Unknown
    # How `base` was reached when the provider does not state it directly (for example
    # "plan price / plan credits / price per 1,000"). None: stated by the provider.
    derived: str | None = None
    rules: list[CostRule] = Field(default_factory=list)  # read top to bottom
    # How several matching rules combine. `multiply_then_add`: a `set` replaces the base,
    # every `multiply` is applied to it, then every `add` is added once.
    stacking: Literal["first_match", "max", "sum", "product", "multiply_then_add", "unknown"] = (
        "unknown"
    )
    # Whether each results page is a request of its own. `included`: several pages come in
    # one request; what a further page adds is a rule with `when: {page: "2+"}`.
    per_page: Literal["separate_request", "included", "unknown"] = "unknown"
    # How many organic results one charge covers (10 where billing is per page of ten).
    results_per_charge: Annotated[int, Field(gt=0)] | Unknown | None = None
    # Parameters the provider documents as free of surcharge. Documentation only.
    free_parameters: list[str] = Field(default_factory=list)
    # Shape keys the documentation does not price. A request that differs from the base
    # on one of these has an unknown cost until it is measured.
    undocumented: list[ShapeKey] = Field(default_factory=list)


class Charge(Strict):
    charged: bool | Unknown | NotApplicable = "unknown"
    how: str | None = None
    source: str | None = None


class Charges(Strict):
    """Whether the provider says it charges for requests that did not return results."""

    failed_request: Charge = Field(default_factory=Charge)  # the provider could not get the page
    timeout: Charge = Field(default_factory=Charge)  # the provider's own deadline passed
    empty_serp: Charge = Field(default_factory=Charge)  # Google has no results for the query
    client_timeout: Charge = Field(default_factory=Charge)  # the caller hung up first
    client_error: Charge = Field(default_factory=Charge)  # a bad request (HTTP 4xx)
    cached_response: Charge = Field(default_factory=Charge)  # served from the provider's cache
    # The HTTP statuses the provider says it bills, when it defines billing that way.
    billed_statuses: list[int] | Unknown | NotApplicable = "unknown"
    notes: str | None = None


class Cache(Strict):
    """Whether the provider may answer from a cache instead of asking Google. A cached
    answer is not a measurement of the provider, so the adapter must switch the cache off;
    `bypass` records how."""

    on_by_default: Tri | NotApplicable = "unknown"
    ttl_seconds: int | None = Field(default=None, gt=0)
    bypass: str | None = None  # the parameter that switches it off, as sent by the adapter
    source: str | None = None


class UsageReporting(Strict):
    """Where the provider reports what it charged. This is what the benchmark reads to
    compare the declared cost with the measured one."""

    response_header: str | None = None  # a header with the cost of this request
    response_field: str | None = None  # a field in the body with the cost of this request
    account_endpoint: str | None = None  # an endpoint with the balance or the units used
    notes: str | None = None


class ConflictStatement(Strict):
    text: str
    source: Annotated[str, Field(pattern=r"^https?://")]


class Conflict(Strict):
    """Two statements by the provider that cannot both be true."""

    topic: str
    statements: list[ConflictStatement] = Field(min_length=2)
    used: str  # which statement this file follows, and why


# --- billing: how units are bought --------------------------------------------------------


class ExpireAfter(Strict):
    expire_after_days: int = Field(gt=0)


UnusedUnits = Literal["expire", "roll_over", "never_expire", "unknown"] | ExpireAfter
# What happens when the plan's units run out before the period ends:
#   block         requests are refused until the next period
#   overage       further units are billed at a published rate (see `overage`)
#   auto_renew    the same plan is bought again at once and the period restarts
#   auto_upgrade  the account is moved to the next plan
OnLimit = Literal["block", "overage", "auto_renew", "auto_upgrade", "unknown"]
Period = Literal["month", "one_time"]


class Limits(Strict):
    concurrency: int | None = None
    requests_per_second: float | None = None
    requests_per_minute: float | None = None
    requests_per_hour: float | None = None
    # A cap per hour given as a share of the plan's monthly units (0.2 = 20% an hour).
    hourly_share_of_plan: float | None = Field(default=None, gt=0, le=1)
    notes: str | None = None


class OverageUnitPrice(Strict):
    unit_price: float = Field(ge=0)  # currency per unit beyond the plan


class OverageRule(Strict):
    # next_plan_rate: extra units are billed at the next plan's unit price.
    # plan_rate: extra units are billed at the unit price of the plan one is on.
    rule: Literal["next_plan_rate", "plan_rate"]


class Addon(Strict):
    """A paid option on top of a plan that changes the price of the plan, not of a request
    (a speed tier, a support package). Documentation only: prices are quoted without it."""

    name: str
    price_multiply: float | None = Field(default=None, gt=0)
    price_add: float | None = None
    notes: str | None = None
    source: str | None = None


class Minimum(Strict):
    amount: float = Field(ge=0)
    period: Period


class Free(Strict):
    units: Annotated[float, Field(ge=0)] | Unknown  # `unknown`: a free tier of unstated size
    period: Literal["once", "month"]
    notes: str | None = None


class NoPlanRate(Strict):
    """Units bought with no plan at all, where a provider sells both ways."""

    unit_price: float = Field(ge=0)  # currency per unit
    name: str = "Pay as you go"
    limits: Limits | None = None
    unused_units: UnusedUnits = "unknown"
    source: str | None = None
    notes: str | None = None


class UnitPlan(Strict):
    """A plan that buys a number of units."""

    id: str
    name: str
    price: float = Field(ge=0)
    period: Period
    units: float = Field(gt=0)
    limits: Limits | None = None
    unused_units: UnusedUnits = "unknown"
    on_limit: OnLimit | None = None  # overrides the purchase-level value for this plan
    # Currency per unit beyond this plan, where each plan has its own overage rate.
    overage_unit_price: float | None = Field(default=None, ge=0)
    # What a month costs when a year is paid at once. Documentation only: `price` is always
    # the price of one month paid monthly, never the annual figure a pricing page may show.
    annual_price_per_month: float | None = Field(default=None, ge=0)
    notes: str | None = None


class PlanUnitsPurchase(Strict):
    """Subscriptions and prepaid packs: a plan buys units, a request costs units."""

    model: Literal["subscription", "prepaid_packs", "pay_as_you_go"]
    pricing: Literal["plan_units"]
    plans: list[UnitPlan] = Field(min_length=1)
    # The same units sold with no plan, at a rate per unit, next to the plans.
    pay_as_you_go: NoPlanRate | None = None
    on_limit: OnLimit = "unknown"
    overage: OverageUnitPrice | OverageRule | None = None
    minimum: Minimum | None = None
    free: Free | None = None
    # With `auto_renew`: the share of the plan's units at which it renews (0.98 when the
    # plan is bought again at 98% used). The rest of the old plan carries over.
    auto_renew_at: float = Field(default=1.0, gt=0, le=1)
    addons: list[Addon] = Field(default_factory=list)
    beyond_published: str | None = None  # what the provider says above its largest plan
    refunds: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _unique_plans(self) -> PlanUnitsPurchase:
        _require_unique([p.id for p in self.plans], "plan id")
        return self


class RateCardShape(Strict):
    when: When
    notes: str | None = None


class RateCardPlan(Strict):
    """A plan that is a budget in currency; each request shape has its own price on it."""

    id: str
    name: str
    price: float = Field(ge=0)
    period: Period
    rates_per_1k: dict[str, Annotated[float, Field(ge=0)]]  # named shape -> currency per 1,000
    limits: Limits | None = None
    unused_units: UnusedUnits = "unknown"
    annual_price_per_month: float | None = Field(default=None, ge=0)  # documentation only
    notes: str | None = None


class RateCardPurchase(Strict):
    """Each request shape has its own price, and that price depends on the plan. The ratio
    between shapes is not constant across plans, so units x unit price cannot express it."""

    model: Literal["subscription", "prepaid_packs", "pay_as_you_go"]
    pricing: Literal["rate_card"]
    shapes: dict[str, RateCardShape] = Field(min_length=1)
    benchmarked_shape: str
    plans: list[RateCardPlan] = Field(min_length=1)
    # `overage` with `{rule: plan_rate}`: spending beyond the plan continues at its rates.
    on_limit: OnLimit = "unknown"
    overage: OverageUnitPrice | OverageRule | None = None
    minimum: Minimum | None = None
    free: Free | None = None
    addons: list[Addon] = Field(default_factory=list)
    beyond_published: str | None = None
    refunds: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RateCardPurchase:
        _require_unique([p.id for p in self.plans], "plan id")
        if self.benchmarked_shape not in self.shapes:
            raise ValueError(f"benchmarked_shape '{self.benchmarked_shape}' is not under `shapes`")
        for plan in self.plans:
            unknown = sorted(set(plan.rates_per_1k) - set(self.shapes))
            if unknown:
                raise ValueError(f"plan '{plan.id}' prices shapes that are not defined: {unknown}")
            if self.benchmarked_shape not in plan.rates_per_1k:
                raise ValueError(f"plan '{plan.id}' has no rate for the benchmarked shape")
        return self


class Tier(Strict):
    """A lower rate reached by money. `basis` says what the money is:

    deposit             one payment of at least `from_amount` into the balance
    monthly_commitment  a subscription of at least `from_amount` a month, spent or not
    monthly_usage       a month's spending at the list rate reaches `from_amount`
    """

    from_amount: float = Field(ge=0)
    basis: Literal["deposit", "monthly_commitment", "monthly_usage"] = "deposit"
    # Exactly one: a factor on every price, or the price of one base request on this tier.
    rate_multiplier: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    name: str | None = None
    limits: Limits | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _one_rate(self) -> Tier:
        if (self.rate_multiplier is None) == (self.unit_price is None):
            raise ValueError("a tier needs exactly one of `rate_multiplier`, `unit_price`")
        return self


class PayAsYouGoPurchase(Strict):
    """A balance in currency. `request_cost.base` is the price of one request."""

    model: Literal["subscription", "prepaid_packs", "pay_as_you_go"]
    pricing: Literal["pay_as_you_go"]
    tiers: list[Tier] = Field(default_factory=list)  # more money buys a lower rate
    minimum_topup: float | None = Field(default=None, ge=0)
    unused_units: UnusedUnits = "unknown"
    minimum: Minimum | None = None
    free: Free | None = None
    limits: Limits | None = None
    beyond_published: str | None = None
    refunds: str | None = None
    notes: str | None = None


Purchase = Annotated[
    PlanUnitsPurchase | RateCardPurchase | PayAsYouGoPurchase,
    Field(discriminator="pricing"),
]


class Change(Strict):
    """One thing that differs from the previous billing entry, with where it was read."""

    what: str  # "Lite plan, monthly price"
    was: str
    now: str
    source: Annotated[str, Field(pattern=r"^https?://")]  # the page that shows the new value
    quote: str | None = None  # the page's own words, kept short


class Review(Strict):
    """The person who compared the entry with its sources and confirmed it."""

    confirmed_by: str
    confirmed_at: dt.date
    notes: str | None = None


class BenchmarkAccount(Strict):
    """The plan the benchmark's own key is on. Disclosed publicly."""

    plan: str  # a plan id, or "unknown" / "n/a"
    notes: str | None = None


class BillingEntry(Strict):
    """How the provider bills from `effective_from` until the next entry."""

    effective_from: dt.date
    checked_at: dt.date  # the day the sources were read
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    sources: list[Annotated[str, Field(pattern=r"^https?://")]] = Field(min_length=1)
    # Sources whose prices are drawn by a script: a plain download of the page does not
    # contain them, so they have to be read in a browser.
    sources_need_browser: list[str] = Field(default_factory=list)
    # How to read the sources next time: which toggle to set, which slider hides plans,
    # which page holds the units per request. Written for whoever re-reads the prices.
    reading_notes: list[str] = Field(default_factory=list)
    prices_exclude_tax: Tri = "unknown"
    meter: Meter
    request_cost: RequestCost
    charges: Charges = Field(default_factory=Charges)
    cache: Cache = Field(default_factory=Cache)
    usage_reporting: UsageReporting = Field(default_factory=UsageReporting)
    purchase: Purchase
    conflicts: list[Conflict] = Field(default_factory=list)
    # What differs from the previous entry. Required on every entry after the first: an
    # entry that does not say what changed, and where that is written, cannot be checked.
    changes: list[Change] = Field(default_factory=list)
    # Who confirmed the entry against its sources. Prices are written by a tool and
    # confirmed by a person; `check-providers` reports an entry nobody confirmed.
    review: Review | None = None
    benchmark_account: BenchmarkAccount | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> BillingEntry:
        unit = self.meter.unit
        kind = self.purchase.pricing
        if kind == "pay_as_you_go" and unit != "usd":
            raise ValueError("pricing `pay_as_you_go` needs meter.unit `usd`")
        if kind == "plan_units" and unit == "usd":
            raise ValueError("pricing `plan_units` needs meter.unit `request` or `credit`")
        if kind == "rate_card" and unit != "request":
            raise ValueError("pricing `rate_card` needs meter.unit `request`")
        account = self.benchmark_account
        if account and account.plan not in ("unknown", "n/a"):
            plan_ids = [p.id for p in getattr(self.purchase, "plans", [])]
            if account.plan not in plan_ids:
                raise ValueError(
                    f"benchmark_account.plan '{account.plan}' is not one of the plans {plan_ids}"
                )
        return self


# --- the file ---------------------------------------------------------------------------


class ProviderConfig(Strict):
    """The contents of `provider.yaml`."""

    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    # Where the provider is in its onboarding. See providers/README.md, "Lifecycle".
    status: Literal["draft", "conformance", "shadow", "published"] = "draft"
    homepage: str | None = None
    docs_url: str | None = None
    product: Product
    terms: Terms = Field(default_factory=Terms)
    auth: Auth = Field(default_factory=Auth)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    billing: list[BillingEntry] = Field(min_length=1)
    adapter: Adapter = Field(default_factory=Adapter)

    @model_validator(mode="after")
    def _terms_allow_it(self) -> ProviderConfig:
        terms = self.terms
        if terms.benchmarking == "prohibited" and not terms.permission and self.status != "draft":
            raise ValueError(
                "terms.benchmarking is `prohibited`: the provider stays in `draft` until "
                "`terms.permission` records a written permission"
            )
        return self

    @model_validator(mode="after")
    def _billing_in_date_order(self) -> ProviderConfig:
        dates = [entry.effective_from for entry in self.billing]
        for earlier, later in zip(dates, dates[1:], strict=False):
            if later <= earlier:
                raise ValueError(
                    "billing entries must be in ascending `effective_from` order "
                    f"({later} comes after {earlier}); append new entries at the end"
                )
        for entry in self.billing[1:]:
            if not entry.changes:
                raise ValueError(
                    f"the billing entry from {entry.effective_from} needs `changes`: what "
                    "differs from the entry before it, with the page that shows it"
                )
        return self

    def billing_at(self, day: dt.date) -> BillingEntry | None:
        """The entry in force on `day`, or None when `day` is before the first entry."""
        current = None
        for entry in self.billing:
            if entry.effective_from <= day:
                current = entry
        return current

    @property
    def current_billing(self) -> BillingEntry:
        """The entry in force today. An entry dated in the future (an announced price
        change) waits its turn; before the first entry, the first one."""
        return self.billing_at(dt.datetime.now(dt.UTC).date()) or self.billing[0]


def _require_unique(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what} '{value}'")
        seen.add(value)
