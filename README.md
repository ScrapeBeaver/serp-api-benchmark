# serp-bench

A reproducible comparison of Google SERP APIs: the same searches, sent to every provider
at the same moment, timed the same way, with every request kept on record so that any
number can be recomputed by anyone.

**Who runs it.** This comparison is run by [ScrapeBeaver](https://scrapebeaver.com), which
is one of the providers measured. It is a reproducible comparison, not an independent
benchmark, and it does not call itself one. What it offers instead of independence is
openness: the code, the configuration, the queries and the per-request records are all
here, the rules are mechanical, and a provider can correct its own adapter by pull
request (see [Right of reply](#right-of-reply)).

## Status

This is an early version. It is honest about what it does and does not measure yet.

Measured today:

- **Delivery**: did a usable answer come back on the first attempt (step 1, below).
- **Latency**: how long it took, and what share of requests were answered within 5, 10
  and 30 seconds.
- **Resolved links**: what share of the result links point at the destination, not at a
  Google redirect (step 2). Pages where every link does are counted next to it.
- **Declared prices**: what a provider's own pricing page says a request costs, as a
  machine-readable contract with sources and dates.
- **Poisoned results**: pages that are not Google's real results for the query (step 3,
  below). For now a page is judged by whether it matches the other providers' pages of the
  same minute; the history check that confirms a mismatch needs several days of runs.
- **AI Overview**: how often the answer to an ordinary search carries the text of Google's
  AI Overview, on the searches that had one. Counted apart from the three steps: see
  "AI Overview" below.

Specified, not built yet:

- usable answers (passed all three steps) as the headline figure: it waits for step 3 to
  have a history behind it and a manual audit of the detector;
- statistics across runs: cluster-aware intervals, paired comparisons;
- the published data set.

Until those exist there are no results here, and nothing in this repository ranks anyone.

## How a run works

1. **The plan.** `bench.yaml` says which providers are measured, when, and how many
   requests of each kind. The planner turns a date and a run number into an ordered list
   of tasks. It is deterministic: the same inputs give the same list on any machine.
2. **A warm-up.** Each provider gets one request first, so that measured requests can
   reuse an open connection. It is recorded and never counted.
3. **A fixed timetable.** Task *i* is sent at *t0 + i × interval*, to every provider at
   the same instant, whether or not earlier answers have arrived. A slow provider does not
   slow the timetable down, so its slow period is measured in full instead of being
   quietly skipped. How late each request left compared with the timetable is recorded.
   After the last task, a first page that matched no other provider's page is asked for
   once more from the same provider (a control repeat, step 3). It is recorded and never
   counted, and it cannot slow a measured request down because it is sent after them.
4. **One attempt.** No retries, no redirects followed. What a single call returns is what
   is measured; a provider's own retries inside that call are part of its answer.
5. **One clock.** The runner times every provider the same way: from sending the request
   to the response headers, and to the last byte of the body. The time limit is 60 seconds.
6. **A record per request.** One line of JSON per (task, provider), written as the run
   goes. Everything reported is computed from those lines.

Runs are spread over the day and the timetable moves one hour later each day, so every
hour of the day is sampled within a few days rather than the same hours forever.

### Void: our problems are not a provider's failures

A request is **void** when the problem is ours: our key is invalid or out of credit
(HTTP 401, 402, 403, or the provider's own way of saying so), the provider does not offer
what the task needs, too many of our own requests to it were still in flight, or the run
was aborted. Void requests are counted, shown in the report, and left out of every share.
The rule is mechanical and lives in one function (`validate.void_reason`); nothing is
excluded by hand.

## AI Overview

Whether a provider's answer to an ordinary search carries Google's AI Overview. It is not
one of the three steps and does not make an answer usable or not.

- **What is asked.** Nothing more than the search itself: the same request as every other
  measurement, with the provider's default settings. Several providers return the overview
  through a second request, an option or another product, often at a price
  (`capabilities.ai_overview_more` in each `provider.yaml`); that path is not sent, so the
  figure says what a customer gets for the price of one search.
- **What counts.** The overview's own text. An adapter only points at the block; the
  shared code (`src/serp_bench/ai_overview.py`) counts its characters and leaves out the
  list of sources, addresses, tokens and ids. Less than 200 characters is a first line or
  a token, not the overview.
- **Which searches.** Google shows an overview for some searches and not for others, and
  one answer cannot tell which. A search is taken to have had one when at least two
  providers returned its text for the same task of the same run.
- **The figure.** Answers that carried the text ÷ this provider's delivered answers to
  those searches. The median length of the text is in the summary next to it
  (`ai_overview_chars`).
- **The price.** The pricing page shows what a search with an overview costs next to a
  plain one where the provider's documentation prices it, and says "not stated" where it
  does not.

## Changes to how numbers are computed

A change is listed here with its date. Nothing in the records changes with any of them:
every figure, before and after, is computed from the same record files, so both can be
recomputed by anyone.

- **2026-11-15. Resolved URLs is the share of links, not of pages.** It was the share of
  successful pages where every result link was resolved, so one missing address counted
  the whole page as lost. It is now the share of result links that are resolved. The
  share of whole pages is still computed (`resolved_pages` in the summary) and still
  decides which pages go on to step 3.
- **2026-11-15. A core list and a wide list that rotates.** Until now one list of eighty
  searches was sent in full every day. Forty of them are now the core, sent every day;
  the other forty and 120 new ones are the wide list, a quarter of which is sent each
  day. The daily volume is the same, 96 a day per provider. Figures before and after are
  over different searches: the core is the part that is comparable throughout.
- **2026-11-15. AI Overview is recorded.** Records written before this release have no
  `ai_overview` field, so the figure starts with the first run after it.
- **2026-11-15. The 95th and 99th percentiles are shown from 50 answers** (they needed
  100 and 500). They appear sooner and are rougher at first: at 50 answers the 95th
  percentile rests on the three slowest ones.

## How each number is computed

This section is generated from the registry in `src/serp_bench/metrics.py`, which is the
same source the report, the logs and the CI summary are built from. `serp-bench metrics`
prints it; a test fails if this text and the code disagree.

<!-- metrics:start (generated by `serp-bench metrics --update-readme`) -->
### Step 1: was a usable answer delivered?

The checks run in this order. The first one that fails names the outcome; a request that passes all of them is `delivered`.

| # | Check | What it verifies | Outcome when it fails |
|---|---|---|---|
| 1 | `dispatched` | The request was sent. It is not sent when the provider does not offer what the task needs, when too many of our own requests to that provider were still in flight, or when the run was aborted. | void (not_supported, client_backpressure or run_aborted) |
| 2 | `response_received` | A complete response arrived within the time limit. No retry is made. | timeout or network |
| 3 | `account_ok` | The response is not about our own key or credit: not HTTP 401, 402 or 403, and not the provider's way of saying our quota is used up. | void (auth_or_credits) |
| 4 | `http_2xx` | The status code is 2xx. Redirects are not followed. | http_429, http_4xx, http_5xx or provider_error (1xx, 3xx) |
| 5 | `json_body` | The body is valid JSON. | invalid_json |
| 6 | `no_provider_error` | The body does not report a failure in the provider's own terms. | provider_error |
| 7 | `parsed` | The body has the structure the provider documents, so the adapter can read it. | malformed_results |
| 8 | `has_results` | There is at least one organic result. | empty_results |
| 9 | `min_organic` | There are at least 3 organic results. | empty_results |
| 10 | `well_formed_results` | At least 3 organic results have a non-empty title and an absolute http(s) URL. A result without them stays in the record, marked, and is judged by later steps; one broken result does not fail an otherwise usable page. | malformed_results |
| 11 | `params_echo` | When the provider says which query and country it used, they are the ones asked for. A provider that does not say passes. | param_mismatch |

### Metrics

| Metric | Meaning | Formula |
|---|---|---|
| Tasks | Requests that count: every request of the run except warm-ups, control repeats and void ones. | `records - warm-ups - void` |
| Void | Requests excluded because the problem was ours: our key or credit, a task the provider does not offer, our own backpressure, an aborted run. Counted, shown, and left out of every share. | `count of records with outcome = void, by reason` |
| Sent late | Tasks our stand sent more than 250 ms after their scheduled time. They stay in every figure; the count shows how punctual the run was. | `count of Tasks with dispatch_lag_ms > 250` |
| Delivered | A usable answer came back on the first attempt: every step-1 check passed. | `records with outcome = delivered / Tasks` |
| Failed, by class | Requests that were not delivered, by the first check that failed. | `records with outcome = <class> / Tasks` |
| Delivered within T seconds | A usable answer came back within T seconds. Slow answers and failures both count against it. | `delivered records with total_ms <= T x 1000 / Tasks` |
| Latency, delivered only | How long delivered answers took, from sending the request to the last byte of the body. Failures are not in this figure, so read it next to Delivered. | `quantile of total_ms over delivered records` |
| Time to a delivered answer, all tasks | The shortest wait after which at least that share of ALL tasks had a usable answer, a failure counting as never answered. It is 'not reached' when fewer tasks than that were delivered, and it always agrees with the deadline shares: p95 is at most 30 s exactly when at least 95% of all tasks were delivered within 30 s. | `inverse of the empirical distribution of total_ms over Tasks, with total_ms = infinity for a failure` |
| Units the responses say were charged | The sum of what each response itself reports as charged, where it says. This is measured, as opposed to the prices a provider declares. | `sum of billed_units over records where it is stated` |

### Methods

- **Interval of a share.** Every share is printed with a Wilson 95% score interval. The intervals assume independent requests; cluster-aware intervals arrive with the statistics module.
- **Quantiles.** Latency percentiles of delivered answers use Hyndman and Fan's type 7 (linear interpolation between the two nearest values, the default of NumPy and R). The all-tasks view contains 'never', which cannot be interpolated, so it uses the inverse of the empirical distribution (type 1): the smallest observed value that at least p% of the tasks did not exceed.
- **Enough data for a quantile.** A quantile is printed only with enough values behind it: p50 needs 20, p90 needs 50, p95 needs 50, p99 needs 50. Otherwise the report says n/a and how many values there were.
- **One attempt.** Every figure describes single calls. The benchmark does not retry; a provider's own retries inside one call are part of what is measured.
<!-- metrics:end -->

### Step 3: is it the page Google really serves?

Google answers traffic it distrusts with a page that looks normal and lists other sites.
Steps 1 and 2 pass on such a page. Nobody can ask Google for the true page, so the check
(`src/serp_bench/poison.py`) rests on how such pages behave: they are random, and real
pages for the same query coincide. Pages are compared as sets of sites (registrable
domains) of their first ten resolved results; order and exact addresses are not compared.

| Signal | Rule |
|---|---|
| A. The query's usual sites | Sites on at least 60% of the reference pages of the last 14 days. A page with 60% of them passes, 40-59% is doubtful, less fails. Reference pages are those of the agreeing group of their own run. Says nothing until a query has 5 reference pages from 3 different days. |
| B. Agreement at the same minute | Two pages agree when at least half of the sites of the shorter one are on the other. The agreeing group is the largest set of pages, from at least two providers, that all agree with each other; it is not a majority vote. A page that agrees with no page of the group fails; one that agrees with some is doubtful. |
| C. The control repeat | A first page outside the group is asked for once more from the same provider after the run. It tells a one-off from a persistent difference and never changes the verdict. |

A page is `clean` when it passes A or is in the group of B, `poisoned` when it fails every
signal that is available, and `unjudged` otherwise. A page that fails A but is in the
group is clean: everyone moved at once, so Google changed. Unjudged pages are reported and
never counted as clean. The share on the results page follows the funnel: poisoned pages
out of the pages that passed step 2 and got a verdict either way.

```bash
uv run serp-bench poison --records out/raw          # per provider, and every poisoned first page
uv run serp-bench poison --records out/raw --all --sites
```

Rule A is the one two rank trackers publish
([DemandSphere](https://www.demandsphere.com/blog/detecting-poisoned-serps),
[Nozzle](https://nozzle.io/research/poisoned-google-serps/)). The check uses no provider's
own detection, the operator's included.

### See it for one request

Every record carries the trail of checks that decided its outcome, in words:

```console
$ serp-bench explain out/raw/2026/11/12/run-0617.jsonl.gz --task 20261112-r1-mobile-004
Task 20261112-r1-mobile-004, provider mock (shadow)
  asked: "what is the difference between baking soda and baking powder", country US, locale en, mobile, page 1
  pool mobile (not in the headline)
  scheduled 2026-11-12T06:17:07.429Z, sent 2026-11-12T06:17:07.430Z
  HTTP 200, first byte 0.18 s, complete in 0.18 s, 120 bytes
  checks, in order:
    PASS  dispatched           sent 2 ms after its scheduled time
    PASS  response_received    complete response in 0.18 s
    PASS  account_ok           not a key or credit problem
    PASS  http_2xx             HTTP 200
    PASS  json_body            valid JSON, 120 bytes
    PASS  no_provider_error    the body reports no failure
    PASS  parsed               the body has the documented shape
    FAIL  has_results          0 organic results
          rule: There is at least one organic result.
  outcome: empty_results (the first check that failed names the outcome)
  the response says 1 unit(s) were charged
```

(The provider in this example is the offline mock.)

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
```

Run the offline mock. No key, no network; it answers deterministically and fails a small
share of requests on purpose, so every part of the pipeline is exercised:

```bash
uv run serp-bench run --providers mock --run 0 --limit 20 --interval 0.2
```

This prints one line per request, then the report, and writes
`out/raw/YYYY/MM/DD/run-HHMM.jsonl.gz`. Recompute the report from the file alone:

```bash
uv run serp-bench verify out/raw/2026/11/12/run-0617.jsonl.gz
```

Run a real provider. Put its key in `.env` (see `.env.example`), check the adapter against
the live service, then run:

```bash
uv run serp-bench providers
uv run serp-bench conformance --provider scrapebeaver
uv run serp-bench run --providers scrapebeaver --run 0 --limit 20 --allow-draft
```

Tasks leave on a timetable, one every 15 seconds by default, so 20 tasks take five
minutes. `--interval SECONDS` changes that for a single run.

`--run next` does the first of today's runs that is not stored yet, so a run started by
hand needs no index. `--run auto` (the default) is what the schedule asks for: the run that
fell due recently and is not stored yet, or nothing. "Recently" is the gap between two runs
less an hour, five hours at four runs a day: a scheduled job on GitHub starts tens of minutes
late, often in the next clock hour, and some of the hourly starts never happen at all (more
than three hours passed between two of them on 2026-11-16). A run must not be lost for that.
`--allow-draft` is needed while a provider's status is `draft`.

### Commands

| Command | What it does |
|---|---|
| `serp-bench providers` | The providers found, their status, key and capabilities |
| `serp-bench plan [--date D] [--run N]` | The tasks a run would send, and the arithmetic of the pools |
| `serp-bench run` | Run once, write records, print the report |
| `serp-bench verify FILE` | Validate a records file and recompute its report |
| `serp-bench poison --records DIR` | Step 3 over recorded runs: which pages matched the others, which did not |
| `serp-bench explain FILE --task ID` | How one request's outcome was decided |
| `serp-bench metrics` | How every reported number is computed |
| `serp-bench conformance --provider X` | Call a provider for real; check the adapter and the declared billing |
| `serp-bench check-providers` | Everything that can be checked about provider folders without a key |
| `serp-bench price --provider X --volume N` | A declared price per 1,000, with its formula |
| `serp-bench schema` | The JSON Schema of `provider.yaml` |

`run` and `conformance` take `--log-level` and `--log-format text|json`. Logs go to
standard error and reports to standard output. No key, header value or response body is
ever logged.

## Configuring what is sent: `bench.yaml`

```yaml
methodology_version: "0.1"
providers: [scrapebeaver]

schedule:
  runs_per_day: 4         # evenly spaced; must divide 24
  minute: 17              # past the hour
  rotate_hours: true      # the timetable moves one hour later each day

pools:
  core:
    source: queries/core.yaml
    per_day: 40           # the whole list every day, spread evenly over the pool's runs
    runs: all             # or run indexes, e.g. [0, 2]
    device: desktop
    page: 1
    headline: true        # counts towards the main table
  wide:
    source: queries/wide.yaml
    per_day: 40           # a quarter of the list a day: every search every four days
    headline: true
  page2:
    from: core            # the same queries as `core`
    per_day: 16
    page: 2

limits:
  timeout_seconds: 60
  dispatch_interval_seconds: 15
  max_in_flight_per_provider: 5
  max_tasks_per_provider_per_run: 130
```

A **pool** is a named stream of tasks: where its queries come from, how many are sent per
day, in which runs, with which device and results page. Only pools marked `headline` feed
the main figures; the others are reported separately. `serp-bench plan` prints what the
file adds up to per run and per day.

A pool walks its query list in passes, and a day takes the next `per_day` queries of that
sequence. The list is cut once into day blocks, in an order shuffled for the pool, and a
query keeps its block: so every query is sent equally often and none is skipped, once a
day when `per_day` equals the size of the list, exactly every fourth day when it is a
quarter of the list. Inside its day the order is shuffled anew on every pass, so a query
does not stay at the same time of day. A longer list with the same `per_day` measures
more different queries for the same number of requests.

All of it follows from the date and from this file: `serp-bench plan --date D --run N`
prints what any run of any day sends, before or after the fact.

Changing this file changes what the numbers mean. Every record therefore carries a hash of
the effective configuration (this file plus the contents of the query files), and a report
warns when it is asked to combine two configurations.

### The searches

Two lists, both in `queries/`:

- **`core.yaml`: forty searches, every one of them every day.** The same searches every
  day are what makes one day comparable with the next, and they give step 3 its history.
- **`wide.yaml`: 160 searches, forty a day**, so every one comes back exactly every four
  days. It keeps the figures from being about forty searches only. Four days is the
  longest gap at which step 3 still has a history for a search: it needs a search on
  three earlier days within fourteen.

About half of the 200 are commercial (loans, insurance, software, shopping), because that
is where poisoning concentrates; the rest are questions, local searches, people and
places, how-to and technical searches, in eight countries and six languages.

Both lists are public, and which searches go on which day follows from the date. That is
what lets anyone recompute a run, and it also means a provider that wanted to treat
exactly these searches differently could. A wide list that rotates makes that a lot of
work instead of a little; it does not make it impossible, and nothing here claims so.

## Providers

A provider is a folder. Adding one does not require changing any other file.

```
providers/<id>/
  provider.yaml     what the provider declares: product, capabilities, billing, sources
  adapter.py        how to ask it and how to read its answer
  fixtures/         responses the adapter must read correctly
  conformance.json  the last live check (written by `serp-bench conformance`)
```

- **`provider.yaml`** holds what the provider *declares* in its documentation and on its
  pricing page: which product is measured and with which settings, what it can do, and how
  it bills, as a dated price history with a source for every entry. A fact is a value,
  `unknown` or `n/a`; nothing is guessed. What a run was *actually* charged is measured and
  lives in records, never in this file.
- **`adapter.py`** is one small class. It says what request to send for a task and reduces
  the response to a common shape. It does not send anything and does not measure anything:
  the runner owns the connection and the clock, so every provider is measured identically.
- **A lifecycle** keeps a new provider out of published numbers until it has been checked
  against the live service: `draft` → `conformance` → `shadow` → `published`.

The contract of `provider.yaml`, the fixture format, the lifecycle and the steps to add a
provider are in [`providers/README.md`](providers/README.md). Start from
`providers/_template/`.

## The record format

One JSON object per line, gzip-compressed, `schema_version: 1`.

| Field | Meaning |
|---|---|
| `run_id`, `run_index` | The run, and which of the day's runs it was |
| `config_hash`, `methodology_version` | The configuration and methodology the run used |
| `pool`, `headline` | The pool the task came from, and whether it feeds the main figures |
| `task_id`, `query_id`, `query` | The task and the query |
| `country`, `locale`, `device`, `page`, `location` | What was asked for |
| `provider`, `provider_status`, `adapter_version` | Who answered, its status at the time, and the adapter that read the answer |
| `scheduled_at`, `started_at` | When the timetable said to send it, and when it was sent (UTC) |
| `dispatch_lag_ms`, `client_lag` | How late it left, and whether that exceeds 250 ms |
| `ttfb_ms`, `total_ms` | Time to the response headers, and to the last byte of the body |
| `http_status`, `bytes` | The status code and the size of the body |
| `outcome` | `delivered`, a failure class, or `void` |
| `void_reason` | Why a void request is void |
| `checks` | The ordered checks that led to the outcome, each with what it saw |
| `organic_count` | How many organic results the answer had |
| `results` | For each result: `pos`, `host`, `url_hash`, `on_google`, `wrapper`, `well_formed` |
| `provider_request_id` | The provider's own id for the request, when it gives one |
| `billed_units` | The units the response itself says were charged, when it says |
| `ai_overview` | The AI Overview block of the answer: `chars`, the characters of its own text, and `deferred`, true when it had no text but a token for a second request. Null when the answer had no such block, and in records written before 2026-11-15 |
| `warmup` | True for the warm-up request, which is never counted |
| `control` | True for a control repeat (step 3), which is never counted |

A record keeps where results point, not what they say: the host and a hash of the
normalised URL, never titles, snippets or bodies. That is enough to recompute every
figure, and it does not republish a provider's output. Every result is kept, including
one whose link is not a usable address. `wrapper` is true when the link is Google's own
redirect or results page (`google.com/goto?url=...`, a relative `/url?q=...`) instead of
the destination; `on_google` is true for any Google host, which also covers real pages
such as support.google.com; `well_formed` says the result has a title and an absolute
http(s) URL.

Records are the contract. Later steps of the methodology are computed from the same
records, so they can be applied to runs made before those steps existed.

## Continuous integration

Four workflows in `.github/workflows/`:

- **`ci.yml`** runs on every push and pull request, with no secrets: lint, tests, a check
  of every provider folder, and a smoke run against the offline mock whose records are
  uploaded as an artifact and whose report is written to the job summary.
- **`run.yml`** is the benchmark itself. On a schedule it wakes up every hour and
  `serp-bench run --run auto` decides whether one of the day's runs is due in that hour
  (the hours move from day to day); otherwise it does nothing. It can also be started by
  hand. It runs real providers with keys from repository secrets, recomputes the report
  from the records file to prove the file is whole, and stores the file on the
  `benchmark-data` branch.
- **`conformance.yml`** is started by hand for one provider. It makes the live conformance
  calls and uploads `conformance.json`.
- **`pages.yml`** builds the results page from the records and publishes it with GitHub
  Pages.

Pull requests from forks never receive secrets.

Each release of the code arrives as a single commit made by a bot, after the tests pass.
The runs, their records and the page are here: `run.yml` writes the records to this
repository directly, and anyone can open the run that produced a file.

### Where the results are kept

Every run writes one file, `raw/YYYY/MM/DD/run-HHMM.jsonl.gz`, to the `benchmark-data`
branch. That branch holds records only and shares no history with the code. A file is
written once and never changed: `scripts/store_run.sh` refuses to replace a run with
different contents. The same script runs in CI and on a laptop.

```bash
uv run serp-bench run --run auto          # what the schedule does
uv run serp-bench verify out/raw/2026/11/12/run-0017.jsonl.gz
scripts/store_run.sh out benchmark-data origin
```

### The results site

`serp-bench site` builds the site from the record files:

```text
dist/
  index.html               results
  pricing/index.html       published prices, by volume
  methodology/index.html   what is measured and how
  summary.json             the data behind all three
  llms.txt  robots.txt  sitemap.xml  CNAME  404.html  assets/
```

```bash
uv run serp-bench site --records out/raw --out dist
python3 -m http.server --directory dist 8766     # then open http://localhost:8766
```

- **One input.** The pages are `site/template.html` plus the summary. `src/serp_bench/summary.py`
  computes every figure from the records and from the provider contracts, with the same
  code for every provider. `src/serp_bench/site.py` only writes what it is given.
- **Three addresses, not three tabs.** Results, pricing and methodology each have their own
  URL, title, description and heading, and carry only their own section, so each can be
  found and linked on its own.
- **The figures are in the HTML.** The main table of each page is written in at build time,
  so it reads without a script. The script then replaces it with the interactive one.
- **What is not measured yet is shown as such.** A step of the methodology that is not
  computed yet appears as "not measured yet", and a figure with too few answers behind it
  as "collecting data". Neither is left out or shown as zero.
- **A provider appears when it is `published`.** Until launch, `site.preview: true` in
  `bench.yaml` shows every measured provider, puts a notice on the pages and asks search
  engines not to index them. Setting it to `false` is the launch.

What is there for search engines and for assistants:

| | |
|---|---|
| `<title>`, description, canonical | One per page, the description written from the data so it names only the providers on the page |
| Structured data | `Organization` (the same `@id` as on scrapebeaver.com) and `WebSite` on every page; `Dataset` on results and pricing; `TechArticle` on the methodology; `BreadcrumbList` below the root |
| Link previews | Open Graph and Twitter tags with `assets/og.png` (1200x630) |
| `robots.txt` | Allows everything and names the crawlers of assistants and answer engines, as the main site does; in a preview, disallows everything |
| `sitemap.xml` | The three pages, with the day of the last build |
| `llms.txt` | The site in plain text: what it is, its pages, and the current figures with the number of searches behind them |
| `summary.json` | The data itself |

`pages.yml` publishes the page with GitHub Pages. The benchmark run starts it whenever it
has stored new records, so the page follows the data within minutes.

To set a repository up:

1. Add each provider's key as an Actions secret under the name in its `provider.yaml`
   (`SCRAPEBEAVER_API_KEY`).
2. Allow Actions to write to the repository (Settings, Actions, General, Workflow
   permissions: read and write), so that a run can push its records.
3. Start a run by hand once (Actions, Benchmark run, Run workflow) and look at its summary.
4. For the page: Settings, Pages, Source: GitHub Actions. Put the custom domain there
   (`benchmark.scrapebeaver.com`) and point a DNS `CNAME` record for it at
   `<owner>.github.io`. With the free plan the repository has to be public.
5. Turn the schedule on by setting the repository variable `BENCHMARK_SCHEDULE` to `on`.
   Until then the hourly schedule does nothing, because every scheduled run spends real
   searches on each provider's account: with the configuration in `bench.yaml`,
   `serp-bench budget` says how many a month.

## Roadmap

1. This version: delivery, latency, declared prices, the provider contract, conformance.
2. More providers, each brought in through the lifecycle above.
3. The benchmark's query set, and scheduled runs with the records published.
4. Link checks and detection of results that are not the real results for the query.
5. Statistics across runs, and the results page.

## Right of reply

If you work for a provider measured here and something is wrong, the adapter, the settings
used, a price, a capability, open an issue or a pull request against your provider's
folder. The fixtures and the conformance command exist so that such a correction can be
checked by anyone. Corrections to past data are made in public commits, never silently.

## Licence

- **Code:** the MIT licence, see [LICENSE](LICENSE).
- **Data** (the records of the runs, the figures computed from them, the list of searches,
  the compiled prices): Creative Commons Attribution 4.0, see
  [LICENSE-DATA.md](LICENSE-DATA.md). Use it freely, with a link to
  <https://benchmark.scrapebeaver.com>.
