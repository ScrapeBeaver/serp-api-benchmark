# Providers

A provider is a folder in this directory. The benchmark finds providers by folder: adding
one does not require changing any file outside it.

```
providers/<id>/
  provider.yaml     what the provider declares
  adapter.py        how to ask it and how to read its answer
  fixtures/         responses the adapter must read correctly
  conformance.json  the last live check, written by `serp-bench conformance`
```

`_template/` is a scaffold to copy. `mock/` is an offline stand-in used for tests and
smoke runs; it is not a real provider and never appears in results.

## Lifecycle

`status` in `provider.yaml` says where a provider is. It only moves forward when the step
before it has actually happened.

| Status | Meaning | `serp-bench run` |
|---|---|---|
| `draft` | The folder exists. The adapter was written from documentation and has not been checked against the live service. | Refused, unless `--allow-draft` |
| `conformance` | `serp-bench conformance` has been run with a real key and nothing failed. `conformance.json` is in the folder. | Runs; reports say "conformance, not published" |
| `shadow` | The provider is measured on the schedule. Its data is collected and not published. | Runs; reports say "shadow, not published" |
| `published` | Its numbers appear in results. | Runs |

The shadow period exists because the first days of a new adapter mostly reveal the
adapter's own mistakes, and those must not be published as the provider's failures.

`serp-bench check-providers` (run in CI, without keys) warns when a `shadow` or
`published` provider has no `conformance.json` or one older than 45 days.

## Adding a provider

1. Copy `_template/` to `providers/<id>/`. The folder name is the provider's `id`.
2. Fill in `provider.yaml` from the provider's documentation and pricing page, following
   "Reading a pricing page: a checklist" below and the nearest file in `_examples/`. Leave
   `status: draft`. Write `unknown` for anything the documentation does not say. Check the
   price it leads to before writing any code:

   ```bash
   uv run serp-bench curve --file providers/<id>/provider.yaml
   ```
3. Implement `adapter.py`: the provider's cheapest real-time Google endpoint, with default
   settings.
4. Add fixtures: at least one delivered answer and one that is not.
5. Run the checks that need no key:

   ```bash
   uv run pytest tests/test_providers.py
   uv run serp-bench check-providers
   uv run serp-bench price --provider <id> --volume 100000
   ```

6. Put the key in `.env`, and call the service for real:

   ```bash
   uv run serp-bench conformance --provider <id>
   ```

   Replace `unknown` values with what the run established, commit `conformance.json`, and
   set `status: conformance`.
7. Add the key to the CI secrets, and its name to the `env:` blocks of the run and
   conformance workflows (secrets are passed by name, one line each).

## `provider.yaml`

The file is validated strictly: a key the contract does not define is an error, with the
path of the key. `serp-bench schema` prints the JSON Schema, for editor autocomplete and
for validation in CI. `_template/provider.yaml` shows every field with comments.

### Principles

1. **Declared, not measured.** The file holds what the provider says in its documentation
   and on its pricing page. What a run was actually charged is measured, and lives in
   records (`billed_units`) and in `conformance.json`. The two are meant to be compared.
2. **A value, `unknown` or `n/a`.** `unknown` means the documentation does not say and it
   has to be measured. Nothing is guessed; an unknown propagates into every figure that
   depends on it instead of silently becoming a number.
3. **Sources and dates.** Every billing entry lists the URLs its numbers come from and the
   day they were read.
4. **Billing is a history.** `billing` is a list in ascending `effective_from` order.
   Entries are appended, never edited: the last one is current, and a figure for a past
   day uses the entry that was in force on that day. The difference between two entries
   is a provider's price change.
5. **A request's cost depends on its shape.** The shape is described with a closed
   vocabulary the benchmark understands.

### Top level

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `id`, `name` | The id equals the folder name |
| `status` | `draft`, `conformance`, `shadow` or `published` (see Lifecycle) |
| `homepage`, `docs_url` | Where to read about it |
| `product` | Exactly what is benchmarked: `name`, `endpoint`, `mode` (`light` or `full`), `settings` (every non-default parameter the adapter sends), `alternatives` (the provider's other Google endpoints and why they were not chosen), `notes` |
| `terms` | What the terms of service say about benchmarking: `url`, `checked_at`, `benchmarking` (`allowed`, `not_mentioned`, `prohibited`, `unknown`), `quote`, `permission` |
| `auth.env` | Names of the environment variables that hold the keys. Never a value |
| `capabilities` | `mobile`, `city_location`, `time_filter`, `web_tab`, `pagination`, `raw_html`, `ai_overview`, `full_mode`: each `true`, `false` or `unknown`. A task that needs a capability declared `false` is not sent to the provider |
| `billing` | The price history (below) |
| `adapter` | Technical settings of `adapter.py`: `version`, `base_url`, `base_url_env`, `options`. Not facts about the provider |

`product.mode` is `light` for the provider's cheapest synchronous Google mode, which is
the one benchmarked. Queues and deferred modes are a different product.

A task that needs a capability declared `false` is not sent to that provider; it is
recorded as void (`not_supported`), never as a failure. A capability declared `unknown`
is tried.

### A billing entry

```yaml
billing:
  - effective_from: 2026-01-01      # the first day this entry applies
    checked_at: 2026-01-01          # the day the sources were read
    currency: USD
    sources: [https://example.com/pricing]
    sources_need_browser: [https://example.com/pricing]
    prices_exclude_tax: unknown
    meter: ...
    request_cost: ...
    charges: ...
    cache: ...
    usage_reporting: ...
    purchase: ...
    conflicts: [...]
    benchmark_account: {plan: lite}
```

An entry dated in the future is an announced price change: it is in the file, and it is
not used until its day.

`sources_need_browser` lists the sources whose prices are drawn by a script. A plain
download of such a page does not contain the numbers (or contains the annual ones), so it
has to be read in a browser. `reading_notes` says how to read the sources next time: which
toggle to set, which slider hides plans, which page holds the units per request.

`checked_at` is the last day the sources were read and found to match the entry. It is the
one field of an existing entry that may be changed: a re-read that finds nothing new moves
it forward. `check-providers` warns when it is more than 45 days old. The procedure for
filling in and re-reading prices is the `provider-prices` skill (`.claude/skills/`).

**`meter`: what the provider counts.** `unit` is `request`, `credit` or `usd`.
`unit_label` is the provider's own word. `shared_across_products` says whether search
draws from the same balance as the provider's other products.

**`request_cost`: units charged for one request, by its shape.**

```yaml
request_cost:
  base: 5                           # the benchmarked mode, desktop, page 1, country-level
  derived: null                     # how `base` was worked out, when it is not stated
  rules:
    - when: {mode: full}
      set: 10
    - when: {device: mobile}
      multiply: 2
    - when: {query_operators: true}
      multiply: 5
    - when: {ai_overview: true}
      add: 5
      applies: if_returned          # charged only when the AI Overview is in the response
    - when: {option: deep_scrape}   # an option the benchmark never sends: documentation
      add: 5
  stacking: multiply_then_add
  per_page: separate_request
  results_per_charge: 10
  free_parameters: [country, language, html]
  undocumented: [location]
```

The shape vocabulary, which is all a `when` may use:

| Key | Values | Base |
|---|---|---|
| `mode` | `light`, `full` | the benchmarked mode |
| `device` | `desktop`, `mobile` | `desktop` |
| `page` | `1`, `"2+"` | `1` |
| `location` | `country`, `city` | `country` |
| `query_operators` | `true`, `false` | `false` |
| `ai_overview` | `true`, `false` | `false` |
| `option` | any name | a provider option the benchmark never sends; such a rule documents a surcharge and never applies |

A rule has exactly one of `set` (the cost becomes this), `add` (this is added) or
`multiply` (the cost is multiplied by this).

- `applies` is `always` (the default), `if_returned` or `unknown`. Some providers charge
  for a feature only when it is actually in the response, or charge and then credit it
  back. The declared cost of such a request is an upper bound, and a quote says so.
- `stacking` says how several matching rules combine: `first_match`, `max`, `sum`,
  `product`, `multiply_then_add`, or `unknown`. `multiply_then_add` is the usual
  documented form, `cost = base x multipliers + additions`: a `set` replaces the base,
  every `multiply` is applied, then every `add` is added once. When two rules match and
  stacking is `unknown`, the cost of that request is unknown.
- `per_page` says whether each results page is a request of its own
  (`separate_request`), comes inside one request (`included`), or is not documented. With
  `included`, what a further page adds is a rule with `when: {page: "2+"}`.
- `results_per_charge` is the number of organic results one charge covers, where billing
  is per page of ten rather than per request.
- `free_parameters` lists what the provider documents as free of surcharge.
- `undocumented` lists shape keys the documentation does not price. A request that
  differs from the base on one of them has an unknown cost until it is measured. A key
  that is neither in a rule nor listed here is taken to cost the same as the base.

**`charges`: does the provider say it charges when no results come back?** Six cases,
each `{charged: true | false | unknown | n/a, how: "...", source: "..."}`:

| Case | Meaning |
|---|---|
| `failed_request` | the provider could not get the page |
| `timeout` | the provider's own deadline passed |
| `empty_serp` | Google has no results for the query |
| `client_timeout` | the caller hung up before the provider answered |
| `client_error` | a bad request (HTTP 4xx) |
| `cached_response` | the answer came from the provider's cache |

`billed_statuses` is the list of HTTP statuses the provider says it bills, for providers
that define billing that way (`[200, 404, 410]`).

**`cache`: may the provider answer from a cache instead of asking Google?**
`on_by_default`, `ttl_seconds`, and `bypass`: the parameter that switches the cache off. A
cached answer is not a measurement of the provider, so when a cache exists the adapter
sends `bypass`, and `product.settings` shows it.

**`usage_reporting`: where the provider reports what it charged.** `response_header` or
`response_field` for the cost of one request, `account_endpoint` for the balance. This is
what `Provider.billed_units` reads, and what makes the declared cost checkable.

**`purchase`: how units are bought.** `model` says how it is sold (`subscription`,
`prepaid_packs`, `pay_as_you_go`). `pricing` says how a price is computed, and selects one
of three kinds:

1. **`plan_units`**: a plan buys a number of units, and a request costs
   `request_cost` units. Credit and request subscriptions, and prepaid packs.

   ```yaml
   purchase:
     model: subscription
     pricing: plan_units
     plans:
       - {id: lite, name: Lite, price: 40, period: month, units: 200000,
          annual_price_per_month: 33, limits: {concurrency: 5}, unused_units: expire}
     on_limit: block                # block | overage | auto_renew | auto_upgrade | unknown
     overage: null                  # {unit_price: 0.0002} | {rule: next_plan_rate} | {rule: plan_rate}
     minimum: {amount: 40, period: month}
     free: {units: 100, period: once}
     addons: [{name: Priority speed, price_multiply: 2}]
     beyond_published: Custom plans through sales.
   ```

   - `price` is always one month paid monthly. A pricing page that opens on annual prices
     shows a lower number; that one goes to `annual_price_per_month`.
   - List every published plan, including those behind a slider.
   - `period: one_time` is a pack bought once. `unused_units` then says how long it lasts:
     `{expire_after_days: 180}`, `never_expire`, or `unknown`.
   - `unused_units` for a monthly plan is `expire`, `roll_over` or `unknown`.
   - `on_limit` says what happens when the units run out: requests are refused (`block`),
     further units are billed (`overage`), the same plan is bought again at once
     (`auto_renew`), or the account moves to the next plan (`auto_upgrade`). A plan may
     override it, and may carry its own `overage_unit_price`.
   - `limits` holds `concurrency`, `requests_per_second`, `requests_per_minute`,
     `requests_per_hour` and `hourly_share_of_plan` (0.2 when at most 20% of the monthly
     units may be used in an hour).
   - `addons` are paid options that change the price of a plan rather than of a request (a
     speed tier that doubles the plan price). They are listed and not included in quotes.

2. **`rate_card`**: a plan is a budget in currency, and each request shape has its own
   price that also depends on the plan. The ratio between shapes is not constant across
   plans, so units times a unit price cannot express it. Requires `meter.unit: request`.

   ```yaml
   purchase:
     model: subscription
     pricing: rate_card
     shapes:
       standard:   {when: {option: proxy_pool_standard}}
       premium:    {when: {option: proxy_pool_premium}}
       premium_js: {when: {option: proxy_pool_premium, mode: full}}
     benchmarked_shape: premium
     plans:
       - {id: p19, name: "$19", price: 19, period: month,
          rates_per_1k: {standard: 0.50, premium: 1.00, premium_js: 1.50}}
     on_limit: overage
     overage: {rule: plan_rate}     # spending beyond the plan continues at its own rates
   ```

3. **`pay_as_you_go`**: a balance in currency. Requires `meter.unit: usd`, and
   `request_cost.base` is then the price of one request.

   ```yaml
   purchase:
     model: pay_as_you_go
     pricing: pay_as_you_go
     tiers:
       - {from_amount: 100, basis: deposit, rate_multiplier: 0.5}
       - {from_amount: 49, basis: monthly_commitment, rate_multiplier: 0.9}
       - {from_amount: 500, basis: monthly_usage, unit_price: 0.0007}
     minimum_topup: 50
     unused_units: never_expire
   ```

   A tier is a lower rate reached by money, and `basis` says what the money is: one
   payment into the balance (`deposit`), a subscription paid every month whether it is used
   or not (`monthly_commitment`), or a month's spending at the list rate
   (`monthly_usage`). The rate is a factor on every price (`rate_multiplier`) or the price
   of one base request on that tier (`unit_price`).

**`conflicts`** records two statements by the provider that cannot both be true (a free
allowance of 100 on one page and 200 on another), each with its URL, and which one the
file follows.

**`changes`** says what differs from the entry before: `what`, `was`, `now`, the `source`
page that shows the new value, and a short `quote` of it. Every entry after the first must
have at least one. It is what a reviewer checks, and what the benchmark's changelog shows.

**`review`** names the person who compared the entry with its sources and confirmed it:
`confirmed_by`, `confirmed_at`. Prices are written by a tool and confirmed by a person.
`check-providers` fails on a price change that nobody confirmed, and warns when a provider
that is being measured has prices nobody confirmed.

**`benchmark_account.plan`** is the plan the benchmark's own key is on (a plan id, or
`unknown`). It is disclosed with the results.

### What is computed from it

Pure functions in `src/serp_bench/pricing.py`, with tests:

- `billing_at(config, day)`: the entry in force on a day.
- `units_for(shape, billing)`: the units one request costs, or `Unknown` with the reason.
- `price_per_1k(shape, monthly_volume, billing)`: the price of 1,000 requests at a
  monthly volume, with the plan used and the formula.
- `price_curve(shape, billing)`: that price at every volume from 1,000 to 100 million a
  month.

The rule for a price is the same for every provider: **the cheapest published way to cover
the volume under that provider's own terms, paid month by month, divided by the volume.**
What "under its own terms" means depends on what the provider does when a plan runs out,
which the contract records in `on_limit`:

| At the limit the provider... | The quote is |
|---|---|
| refuses requests (`block`), or does not say (`unknown`) | the cheapest plan that includes the whole volume; a plan larger than needed is paid in full |
| bills further units (`overage`) | that, or a smaller plan plus its overage, whichever is less |
| buys the plan again (`auto_renew`) | that, or a smaller plan bought as many times as it takes, whichever is less |
| has no plan (`pay_as_you_go`, packs) | what the volume itself costs |

So 250,000 searches cost a plan of 100,000 plus 150,000 of overage at one provider, and
the whole next plan at another, because that is what each would charge. A quote says which
it is and takes the bill apart: `plan_price`, `extra_cost` and what the extra is.
`--plan-only` counts whole plans only, to see how much the terms at the limit change.

Annual prepayment, negotiated rates and add-ons are left out.

- `plan_units`, monthly plans: as in the table. "Extra units at the next plan's rate"
  prices only the stretch up to the next plan's size. Beyond the largest published plan,
  with no published overage, the price is **not published**: it is never extrapolated.
- `plan_units` with `pay_as_you_go`: the provider also sells the same units with no plan,
  at a rate. The cheaper of the two is taken.
- `plan_units`, packs bought once: a pack is spread over the months it lasts at the
  volume, up to its expiry and never over more than 12 months. A 50,000 pack used at
  10,000 a month costs a fifth of its price a month; used at 1,000 a month it would last
  50 months, so with a six-month expiry its whole price buys six months. A pack with no
  stated expiry counts for one month.
- `rate_card`: the cheapest plan whose budget covers the volume at that plan's rate for
  the shape, or, where spending continues at the plan's rates, the cheapest total.
- `pay_as_you_go`: volume times the price of a request, on the cheapest tier. A commitment
  is paid in full even when less is used. A deposit is spread like a pack: over the months
  it lasts, 12 at most.

One price at one volume says little: a provider that is the dearest at 10,000 searches a
month can be the cheapest at a million. So every provider is quoted at four monthly
volumes (10,000, 100,000, 1 million, 10 million), and the page draws the whole curve.

```console
$ serp-bench curve
Declared price of 1,000 searches by monthly volume, in force on 2026-11-12.
Base shape: the benchmarked mode, desktop, page 1, country-level location.
Rule: the cheapest published way to cover the volume under the provider's own
terms (a plan, or a smaller plan plus its overage or early renewal), per 1,000.

Provider      10,000 / month   100,000 / month  1,000,000 / month  10,000,000 / month
------------  ---------------  ---------------  -----------------  ------------------
ScrapeBeaver  $2.50 (Starter)  $0.80 (Growth)   $0.60 (Business)   not published
```

`serp-bench curve --ladder` prints every step from 10,000 to 100 million a month, which is
the table the page shows. `serp-bench curve --json` prints every point of the curve, with the plan and the notes
behind each. `--file path/to/provider.yaml` prices any contract file, with no adapter
needed, which is how a new provider's pricing is checked before any code is written.

```console
$ serp-bench price --provider scrapebeaver --volume 50000
ScrapeBeaver: Google Search (light)
Billing in force on 2026-11-12 (from 2026-11-11, read 2026-11-11)
Shape: mode light (benchmarked), desktop, page 1, country-level location
Cost of one request: 1 search
Price per 1,000 at 50,000 a month: $0.90
  50,000 requests x 1 per request = 50,000 searches; Starter: $25.00 for 25,000 searches + 25,000 over the plan at $0.000800 each = $45.00 a month; $45.00 / 50,000 x 1,000 = $0.90
  note: uses overage
Sources: https://scrapebeaver.com/pricing, https://scrapebeaver.com/docs/billing
Declared by the provider; what a run was actually charged is in the records.
```

### Reading a pricing page: a checklist

Twenty providers were surveyed before this contract was fixed, and the same traps came up
again and again. `_examples/` holds a contract for each pricing model met there, to copy
from. Before a billing entry is believed, each of these has an
answer or an explicit `unknown`:

1. **Which endpoint?** Several products of one provider may return Google results at
   different prices. The cheapest synchronous one that returns parsed organic results is
   benchmarked; the others go under `product.alternatives` with the reason.
2. **What does one search cost on that endpoint, in the provider's unit?** Not "from
   $0.25 per 1,000": the units per request, from the documentation.
3. **Which defaults are the dear ones?** A parameter left out may select the premium
   rate. `product.settings` lists what the adapter sends to get the base price.
4. **Monthly or annual?** Toggle the page to monthly. Record the monthly price.
5. **Are there plans behind a slider, a dropdown or a "show more"?** List them all.
6. **What multiplies or adds?** Mobile, a second page, a city, search operators, the AI
   Overview, JavaScript rendering. And how do two of them combine?
7. **Charged always, or only when returned?**
8. **What is billed besides a good answer?** Empty pages, 404s, timeouts on either side,
   cached answers.
9. **Is there a cache, and how is it switched off?**
10. **What happens to unused units, and what happens at the limit?**
11. **Where does the provider report the charge?** A header, a field, an account endpoint.
12. **What do the terms say about benchmarking?** `terms.benchmarking: prohibited` keeps
    the provider in `draft` until a written permission is recorded.
13. **Do two pages disagree?** Record both under `conflicts`.

## `adapter.py`

One subclass of `serp_bench.provider.Provider`. The registry requires exactly one per
folder.

| Method | Required | What it does |
|---|---|---|
| `build_request(task)` | yes | The HTTP request for a task: method, URL, headers, query parameters, JSON body |
| `parse(raw)` | yes | Reduce a 2xx JSON response to `Serp`: organic results (position, URL, title, snippet, displayed URL), the provider's request id, and the query and country it says it used |
| `classify_error(raw)` | no | A failure reported inside a 2xx body |
| `own_fault(raw)` | no | A void reason when the response means our own quota is used up in a form other than HTTP 401, 402 or 403 |
| `billed_units(raw)` | no | The units the response itself says were charged |

**An adapter is a translator and nothing else.** It turns a task into the provider's
request and the provider's response into the common shape. Everything that decides a
number is shared code, the same for every provider: sending, timing, the time limit, the
checks that make a request a success, URL resolution, poisoning, percentiles, intervals,
prices. No provider, the one that runs this benchmark included, has a line of its own in
any of that. Three things enforce it:

- `tests/test_providers.py` fails if an adapter overrides anything but the five methods
  above, or imports the modules that measure.
- An adapter can set a request aside as "our own fault" for one reason only, our key or
  credit. Any other reason it returns is ignored. Requests set aside are counted and shown
  per provider, never hidden.
- Fixtures pin what each adapter reads from a recorded response, and the raw responses of
  a run are kept, so what an adapter read can be checked against what was sent back.

Rules for adapters:

- **An adapter does not send and does not measure.** The runner owns the connection, the
  clock and the time limit.
- **Read secrets only with `self.secret(NAME)`**, where `NAME` is declared under
  `auth.env`. That is what guarantees they are redacted from anything printed or stored.
- **Index required fields with `[...]`.** A response without them then raises, and is
  recorded as `malformed_results` instead of being read as an empty answer.
- **Do not repair the response.** If a result has no URL, return it without one: deciding
  what that means is the benchmark's job, in one place, the same for everyone.

## Fixtures

A fixture is a response the adapter must read as expected: a JSON file in
`providers/<id>/fixtures/`.

```json
{
  "description": "A successful search.",
  "source": "Recorded on 2026-11-12, trimmed to three results.",
  "task": {"query": "coffee shops london", "country": "GB", "locale": "en", "device": "desktop", "page": 1},
  "response": {"status": 200, "headers": {"content-type": "application/json"}, "body": {}},
  "expect": {"outcome": "delivered", "organic_count": 3, "billed_units": 1.0}
}
```

- `source` says where the response comes from. A fixture built from documentation rather
  than recorded says so.
- `response.body` is the JSON body; use `response.text` for a body that is not JSON.
- `expect.outcome` is required. `void_reason`, `organic_count`, `billed_units` and
  `provider_request_id` are checked when present.
- A provider that needs a key must ship at least one `delivered` fixture and one that is
  not.
- Remove anything personal or secret before committing a recorded response, and trim it:
  a fixture needs the shape, not ten results.

The same check runs in `tests/test_providers.py` and in `serp-bench check-providers`.

## Conformance

`serp-bench conformance --provider <id>` calls the service: six probes, one request each.

| Probe | What is sent | Sent when |
|---|---|---|
| `desktop_page1` | An ordinary desktop search | always |
| `mobile` | The same search as a phone | `capabilities.mobile` is not `false` |
| `page2` | The second results page | `capabilities.pagination` is not `false` |
| `city_location` | A search from a place below country level | `capabilities.city_location` is not `false` |
| `search_operators` | A query with a `site:` operator | always |
| `no_results` | A quoted nonsense phrase | always |

For each probe it reports two verdicts:

- **Response.** Was the answer what the probe expects? `PASS`, `FAIL`, or `UNKNOWN` when
  it cannot be told (our own key was refused; a capability declared `unknown` did not
  work, which could be either unsupported or a failure). For `no_results` the query is a
  quoted nonsense phrase that matches nothing. An empty results page is a `PASS`. Results
  are `UNKNOWN`, recorded as a finding: Google itself substitutes results for the words
  without quotes, so a provider that passes them on has copied the page, but the results
  are not for the query asked and it matters whether the response says so. An error
  instead of a page is `UNKNOWN` as well.
- **Billing.** Do the units the response says were charged match what `provider.yaml`
  declares for that request shape (and, for a request without results, the declared
  `charges`)? `UNKNOWN` when the response does not say, or the declaration does not.

Conformance passes when nothing is `FAIL`. The table is printed and written, without
secrets or bodies, to `providers/<id>/conformance.json`.
