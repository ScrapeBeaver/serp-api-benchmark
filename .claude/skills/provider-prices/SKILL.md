---
name: provider-prices
description: Write or refresh the declared prices of a SERP API provider in its provider.yaml. Use when asked to add a provider's pricing, to update, refresh or re-check provider prices, when `serp-bench check-providers` warns that prices are stale, or before the benchmark page is published. Works on one provider or on all of them.
---

# Provider prices

A provider's prices live in the `billing` list of its `provider.yaml` (`providers/<id>/`, or
`providers/_examples/<id>.yaml` for a contract with no adapter yet). Everything the
benchmark says about price is computed from that file by `serp-bench curve` and
`serp-bench price`. This skill fills the file in from the provider's own pages, or re-reads
those pages and records what changed.

These numbers are published claims about other companies, next to ours. A wrong one is
worse than a missing one. So: read, do not guess; quote the page; and let a person confirm
every change before it is written.

## Before you start

Read, in this order:

1. `providers/README.md`, from "`provider.yaml`" to the end of "Reading a pricing page: a
   checklist". It defines every field and the pricing rule.
2. `providers/_template/provider.yaml`: every field with a comment.
3. The example in `providers/_examples/` whose pricing model is nearest:

   | Model | Example |
   |---|---|
   | Monthly plans in requests, hourly cap, speed add-ons | `serpapi.yaml` |
   | Monthly plans in credits, multipliers per request | `providers/scrapingdog/provider.yaml` |
   | Credits, a mode flag, early renewal, billed HTTP statuses | `providers/scrapingbee/provider.yaml` |
   | A rate card: the price of a request depends on the plan | `providers/decodo/provider.yaml` |
   | Prepaid packs that expire | `serper.yaml` |
   | A dollar balance, price per page, formula with multipliers | `dataforseo.yaml` |
   | A dollar balance with a monthly commitment that buys a discount | `scrapeless.yaml` |
   | Pay as you go next to plans with overage | `brightdata.yaml` |
   | Automatic overage at a rate per plan | `serpstack.yaml` |
   | A surcharge only when the feature is returned, a free cache | `zenserp.yaml` |
   | Additive add-ons, terms that forbid benchmarking | `cloro.yaml` |

## Rules

- **Only the provider's own pages are sources**: pricing page, documentation, help centre,
  terms. Never a review site, a comparison article or memory.
- **A fact is a value, `unknown` or `n/a`.** If the page does not say, write `unknown`. A
  value worked out by arithmetic goes with how it was worked out (`derived`, or a note).
- **Every billing entry carries its URLs and the day they were read.** Put the URL on the
  rule or the charge it supports wherever the field has `source`.
- **`price` is one month paid monthly.** Switch any annual toggle off before reading. The
  annual figure goes to `annual_price_per_month`.
- **List every plan**, including those behind a slider, a dropdown or a "show more".
- **Open pages listed in `sources_need_browser` in a browser.** A plain download of such a
  page misses the prices or shows the annual ones. When you find a new page like that, add
  it to the list.
- **Two pages disagree? Record both under `conflicts`** and say which one the file follows.
- Do not sign in anywhere, create accounts, or enter a key. Decline cookie banners.
- Do not edit `src/`, tests or the template as part of this. If the contract cannot
  express something, write it in the nearest `notes` field and say so in the report.

## A person confirms every price

Nothing about a price is final until a person has looked at the page and said yes. You
read, compare and propose; they confirm. This holds for a new provider and for every
change to an existing one.

- **Do not write a price change into `provider.yaml` before it is confirmed.** Keep the
  proposal in your reply (and, for a long one, in a scratch file).
- **Show each change so it can be checked in a minute**: the provider, what changed, the
  old value, the new value, a link that opens the page where the new value is written (to
  the section, where the page has anchors), and the page's own words in a short quote.
- **Ask for the decision per provider**: confirm, reject, or "I will look myself". Use the
  question tool when there is one; otherwise ask in the reply and stop.
- **Write only what was confirmed.** A rejected change is dropped and reported. For
  "I will look myself", wait.
- **Record who confirmed**: `review: {confirmed_by: <the person>, confirmed_at: <today>}`.
  Ask for the name or handle to write; never put a tool or a model there.
- **Running with nobody to ask** (a scheduled job, CI): change nothing. Write the proposal
  as the report and end. A later run with a person present applies it.

## Mode 1: a new provider

1. Find the endpoint: the provider's cheapest synchronous Google web search that returns
   parsed organic results with their addresses. Other endpoints of the same provider that
   return Google go under `product.alternatives` with the reason.
2. Work through the thirteen questions of the checklist in `providers/README.md`. Each
   gets an answer with a URL, or `unknown`.
3. Write the file from the template: `status: draft`, `effective_from` and `checked_at`
   today, no `review` yet. Fill `reading_notes` with what the next reader needs to know
   about these pages (which toggle, which slider, where the units per request are written).
4. Read the terms of service for a clause on benchmarking or competitive analysis and fill
   `terms`. `prohibited` keeps the provider in `draft`: say so in the report.
5. Check the result (below).
6. Show the person the price table and, for every plan, rule and charge, the link it was
   read from. When they confirm, add `review`. Until then the file stays as a draft
   without it, and `check-providers` will say so once the provider is measured.

## Mode 2: re-reading prices

1. Save the price before: `uv run serp-bench curve --ladder --json --file <file>`.
2. Open every URL in the current entry's `sources`, following its `reading_notes`.
3. Compare with the current entry, field by field: plans (name, monthly price, units,
   limits), cost of one request and every rule, charges, cache, unused units, what happens
   at the limit, free tier, terms. Edit nothing yet.
4. **Nothing changed:** set `checked_at` of the current entry to today. That is the only
   edit an existing entry may get, and it needs no confirmation. Say in the report which
   pages were compared.
5. **Something changed:** build the proposal and ask, as described above:

   ```text
   Scrapingdog: 2 changes found on 2026-12-01

   1. Lite plan, monthly price: $40 -> $45
      https://www.scrapingdog.com/pricing/
      "Lite $45 /month"
   2. Standard plan, credits a month: 1,000,000 -> 1,200,000
      https://www.scrapingdog.com/pricing/
      "1.2M credits"

   Price per 1,000 searches    10k      100k     1M       10M
   before                      $4.00    $0.90    $0.35    $0.25
   after                       $4.50    $0.90    $0.35    $0.25

   Confirm these changes for Scrapingdog?
   ```

6. **After a yes:** append a new entry. Copy the current one, change what changed, set
   `checked_at` to today and `effective_from` to the day the provider says the change took
   effect, or today when it does not say. Fill `changes` with one item per confirmed
   change (`what`, `was`, `now`, `source`, `quote`) and `review` with who confirmed. A
   price the provider has announced for a future date is an entry with that future date.
   Never rewrite the old entry: the difference between two entries is the record of the
   change, and `changes` is what the benchmark's changelog shows.
7. **A page is gone, or cannot be read:** leave the values, keep the old `checked_at`, and
   report it with the link that failed. Do not fill the gap from another site.

## Check the result

```bash
uv run serp-bench curve --file <file> --volumes 10k,100k,1M,10M
uv run serp-bench price --file <file> --volume 100000
uv run serp-bench price --file <file> --volume 100000 --device mobile
uv run serp-bench check-providers
uv run pytest tests/test_examples.py tests/test_providers.py -q
```

The quote follows the provider's own terms at the limit (`on_limit`): a plan that includes
the volume, or a smaller plan plus overage or early renewal where the provider has them
and it comes to less. So `on_limit`, `overage` and `overage_unit_price` decide real
numbers: read them from the page as carefully as the plan prices. Read each formula that
`price` prints and ask whether a buyer would really be charged that. If the provider's own page states a price per 1,000 for a plan, the curve must agree
with it at that plan's size; a disagreement means the units per request are wrong.

## Report

For each provider:

- each change: old value, new value, the link and the quote; whether it was confirmed,
  rejected or is waiting, and by whom;
- the pages that were compared and found unchanged, as links, so the reader can see what
  "up to date" rests on;
- the price per 1,000 at 10k, 100k, 1M and 10M searches a month, before and after;
- every `unknown` that is new, every conflict found, every page that could not be read;
- anything the contract could not express.

Do not commit and do not open a pull request unless asked. When a pull request is opened,
its description carries the same list of changes with their links.
