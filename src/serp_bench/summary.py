"""The summary the results page is built from: one JSON document, computed from the records
of the runs and from what each provider declares.

Everything on the page comes from here, and everything here comes from two places only: the
record files (what was measured) and the provider contracts (what is declared). The same
functions measure every provider; nothing in this module knows a provider by name.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from serp_bench.contract import BillingEntry, ProviderConfig
from serp_bench.models import OUTCOME_DELIVERED, OUTCOME_VOID, Record
from serp_bench.poison import CLEAN, POISONED, UNJUDGED, Key, Verdict, judge
from serp_bench.poison import HISTORY_DAYS as POISON_HISTORY_DAYS
from serp_bench.pricing import (
    HEADLINE_VOLUMES,
    LADDER_VOLUMES,
    Shape,
    Unknown,
    cost_ratio,
    price_curve,
    shape_for,
)
from serp_bench.records import read_records
from serp_bench.stats import MIN_N_FOR_QUANTILE, quantile, wilson_interval

SUMMARY_SCHEMA = 1
WINDOWS: tuple[int, ...] = (1, 7, 30, 90)  # days, ending on the day of the latest run
HISTORY_DAYS = 90
# Steps of the methodology that are not computed yet. The page shows them as "not measured
# yet" instead of leaving them out, so that it is plain what is still missing.
#
# Usable answers (passed all three steps) wait for step 3 to be more than an experiment:
# two weeks of history behind the verdicts, and a manual audit of the detector.
NOT_MEASURED_YET: tuple[str, ...] = ("usable",)
# Step 3 is judged on history once this part of the judged pages of the last two weeks had
# a history to be judged on. Until then the page calls the figure a mismatch with the other
# providers, not poisoning.
HISTORY_READY_SHARE = 0.9
# Response times are given to the page as they are, so that it can count the share of all
# searches answered within any number of seconds. Above this many they are given as evenly
# spaced quantiles instead, and the page is told so (`exact: false`).
LATENCY_EXACT_MAX = 2000
LATENCY_POINTS = 201
# The figures of one run that the page draws over time.
RUN_FIGURES: tuple[str, ...] = (
    "tasks",
    "success",
    "resolved",
    "resolved_pages",
    "poisoned",
    "p50",
    "p95",
)

# The request shapes whose cost is shown next to a plain desktop search.
COMPARED_SHAPES: dict[str, dict[str, object]] = {
    "mobile": {"device": "mobile"},
    "page2": {"page": 2},
    "full": {"mode": "full"},
    "operators": {"query_operators": True},
}


def load_records(folder: Path) -> list[Record]:
    """Every record of every run file under `folder`."""
    records: list[Record] = []
    for path in sorted(folder.rglob("run-*.jsonl.gz")):
        records.extend(read_records(path))
    return records


def day_of(record: Record) -> dt.date | None:
    """The UTC day a request belongs to: when it was due, or when it was sent."""
    stamp = record.scheduled_at or record.started_at
    if stamp is None:
        return None
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(dt.UTC).date()


# --- what is measured --------------------------------------------------------------------


def _share(successes: int, n: int) -> list[float] | None:
    """[percent, low, high, successes, n] with the 95% Wilson interval; None without data."""
    interval = wilson_interval(successes, n)
    if interval is None:
        return None
    low, high = interval
    return [round(100 * successes / n, 2), round(100 * low, 2), round(100 * high, 2), successes, n]


def _percentile(seconds: list[float], p: int) -> list[float] | None:
    """[seconds, n], or None when there are too few answers for this percentile."""
    if len(seconds) < MIN_N_FOR_QUANTILE[p]:
        return None
    value = quantile(seconds, p)
    assert value is not None
    return [round(value, 3), len(seconds)]


def resolved(record: Record) -> bool:
    """Step 2 for one page: every organic result points at its destination, none at Google's
    own redirect. This is what lets a page go on to step 3."""
    return not any(result.wrapper for result in record.results)


def link_counts(records: Iterable[Record]) -> tuple[int, int]:
    """Step 2 by links: (resolved organic results, organic results) over these pages."""
    links = [result for record in records for result in record.results]
    return sum(1 for link in links if not link.wrapper), len(links)


Verdicts = Mapping[Key, Verdict]


def _verdict(record: Record, verdicts: Verdicts) -> str | None:
    """Step 3 for one delivered page: clean, poisoned or unjudged; None when not judged."""
    verdict = verdicts.get((record.run_id, record.task_id, record.provider))
    return None if verdict is None else verdict.verdict


def measure(records: Iterable[Record], verdicts: Verdicts | None = None) -> dict[str, Any]:
    """The figures of one provider over one set of requests. Void requests (our own key or
    credit, a task the provider does not offer) are counted and left out of every share.

    Step 2 is reported twice. `resolved` is the share of result links that point at their
    destination, over the delivered pages: nine addresses out of ten is nine tenths. Its
    interval treats links as independent, which links of one page are not, so it is
    narrower than it should be. `resolved_pages` is the share of delivered pages where
    every link is resolved.

    `verdicts` are the step-3 verdicts (`poison.judge`). The steps are a funnel, so step 3
    is counted on the pages that passed step 2 whole: the poisoned share is poisoned pages
    out of the fully resolved pages that got a verdict either way. Pages that could not be
    judged are left out of it and reported next to it, as a share of those pages.
    """
    records = list(records)
    counted = [r for r in records if r.outcome != OUTCOME_VOID]
    delivered = [r for r in counted if r.outcome == OUTCOME_DELIVERED]
    seconds = [r.total_ms / 1000 for r in delivered if r.total_ms is not None]
    good = [r for r in delivered if resolved(r)]
    step3 = Counter(_verdict(r, verdicts or {}) or UNJUDGED for r in good)
    figures: dict[str, Any] = {
        "tasks": len(counted),
        "void": len(records) - len(counted),
        "success": _share(len(delivered), len(counted)),
        "resolved": _share(*link_counts(delivered)),
        "resolved_pages": _share(len(good), len(delivered)),
        "poisoned": _share(step3[POISONED], step3[CLEAN] + step3[POISONED]),
        "unjudged": _share(step3[UNJUDGED], len(good)) if verdicts else None,
        "p50": _percentile(seconds, 50),
        "p95": _percentile(seconds, 95),
    }
    figures.update(dict.fromkeys(NOT_MEASURED_YET))
    return figures


def moment_of(record: Record) -> dt.datetime | None:
    """When a request was due, or when it was sent, in UTC."""
    stamp = record.scheduled_at or record.started_at
    if stamp is None:
        return None
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(dt.UTC)


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_list(records: Iterable[Record]) -> list[dict[str, str]]:
    """The runs behind the figures, oldest first: each with the moment its first task was
    due and the moment its last one was."""
    spans: dict[str, list[dt.datetime]] = {}
    for record in records:
        moment = moment_of(record)
        if moment is None:
            continue
        span = spans.setdefault(record.run_id, [moment, moment])
        span[0], span[1] = min(span[0], moment), max(span[1], moment)
    ordered = sorted(spans.items(), key=lambda item: item[1][0])
    return [
        {"id": run_id, "start_at": _stamp(first), "end_at": _stamp(last)}
        for run_id, (first, last) in ordered
    ]


def latency(records: Iterable[Record]) -> dict[str, Any]:
    """The response times of the delivered answers, in seconds and in order, with the
    number of searches they are out of. A search that was not delivered has no time, which
    is how a share "answered within T seconds" comes to count failures against it."""
    counted = [r for r in records if r.outcome != OUTCOME_VOID]
    seconds = sorted(
        round(r.total_ms / 1000, 3)
        for r in counted
        if r.outcome == OUTCOME_DELIVERED and r.total_ms is not None
    )
    delivered = len(seconds)
    exact = delivered <= LATENCY_EXACT_MAX
    if not exact:
        last = delivered - 1
        picked = []
        for step in range(LATENCY_POINTS):
            at = last * step / (LATENCY_POINTS - 1)
            low = int(at)
            high = min(low + 1, last)
            picked.append(round(seconds[low] + (seconds[high] - seconds[low]) * (at - low), 3))
        seconds = picked
    return {"seconds": seconds, "exact": exact, "delivered": delivered, "tasks": len(counted)}


def _outcomes(records: list[Record], verdicts: Verdicts) -> dict[str, Any]:
    """Every request in exactly one outcome, the first step it did not pass: not delivered,
    links not resolved, poisoned, could not be judged; otherwise clean."""
    counted = [r for r in records if r.outcome != OUTCOME_VOID]
    delivered = [r for r in counted if r.outcome == OUTCOME_DELIVERED]
    good = [r for r in delivered if resolved(r)]
    step3 = Counter(_verdict(r, verdicts) or UNJUDGED for r in good)
    failed = Counter(r.outcome for r in counted if r.outcome != OUTCOME_DELIVERED)
    return {
        "tasks": len(counted),
        "resolved": len(good),
        "clean": step3[CLEAN],
        "poisoned": step3[POISONED],
        "unjudged": step3[UNJUDGED],
        "unresolved": len(delivered) - len(good),
        "failed": sum(failed.values()),
        "failed_by_class": dict(sorted(failed.items())),
    }


def poison_stage(records: Iterable[Record], verdicts: Verdicts, last: dt.date) -> dict[str, Any]:
    """What step 3 stands on for the pages of the last two weeks: `agreement` while the
    verdicts come from the providers agreeing with each other, `history` once nearly all
    of them were also judged against the query's usual sites."""
    first = last - dt.timedelta(days=POISON_HISTORY_DAYS - 1)
    recent = [
        verdict
        for record in records
        if (day := day_of(record)) is not None
        and first <= day <= last
        and (verdict := verdicts.get((record.run_id, record.task_id, record.provider)))
    ]
    judged = [verdict for verdict in recent if verdict.verdict != UNJUDGED]
    with_history = sum(verdict.history is not None for verdict in judged)
    ready = bool(judged) and with_history / len(judged) >= HISTORY_READY_SHARE
    return {
        "stage": "history" if ready else "agreement",
        "history_days": POISON_HISTORY_DAYS,
        "judged": len(judged),
        "judged_with_history": with_history,
        "unjudged": len(recent) - len(judged),
    }


def _provider(
    config: ProviderConfig,
    records: list[Record],
    days: list[dt.date],
    operator: str | None,
    verdicts: Verdicts,
    runs: list[dict[str, str]],
) -> dict[str, Any]:
    """One provider's entry: its figures for each window, day by day, run by run, and by
    outcome."""
    by_day: dict[dt.date, list[Record]] = {}
    for record in records:
        day = day_of(record)
        if day is not None:
            by_day.setdefault(day, []).append(record)
    last = days[-1]

    def within(n: int) -> list[Record]:
        first = last - dt.timedelta(days=n - 1)
        return [r for day, rows in by_day.items() if first <= day <= last for r in rows]

    daily: dict[str, list[float | None]] = {
        key: []
        for key in (
            "success",
            "resolved",
            "resolved_pages",
            "poisoned",
            "p50",
            "p95",
            *NOT_MEASURED_YET,
        )
    }
    for day in days:
        figures = measure(by_day.get(day, []), verdicts)
        for key, series in daily.items():
            value = figures[key]
            series.append(None if value is None else value[0])

    by_run: dict[str, list[Record]] = {}
    for record in records:
        by_run.setdefault(record.run_id, []).append(record)

    def of_run(run_id: str) -> dict[str, Any] | None:
        """This provider's figures in one run; None when it took no part in it."""
        rows = by_run.get(run_id)
        if not rows:
            return None
        figures = measure(rows, verdicts)
        return {key: figures[key] for key in RUN_FIGURES}

    account = config.current_billing.benchmark_account
    return {
        "id": config.id,
        "name": config.name,
        "self": config.id == operator,
        "site": urlparse(config.homepage or "").netloc.removeprefix("www."),
        "status": config.status,
        "plan": account.plan if account else None,
        "product": config.product.name,
        "windows": {str(n): measure(within(n), verdicts) for n in WINDOWS},
        "daily": daily,
        "outcomes": _outcomes(within(30), verdicts),
        "runs": [of_run(run["id"]) for run in runs],
        "latency": {str(n): latency(within(n)) for n in WINDOWS},
    }


# --- what is declared ----------------------------------------------------------------------


def per_search(config: ProviderConfig, billing: BillingEntry) -> dict[str, object]:
    """For each compared shape: how many times a plain search it costs, `not_offered`, or
    None when the provider does not say."""
    offered = {
        "mobile": config.capabilities.mobile,
        "page2": config.capabilities.pagination,
        "full": config.capabilities.full_mode,
    }
    result: dict[str, object] = {"desktop": 1.0}
    for key, kwargs in COMPARED_SHAPES.items():
        if offered.get(key) is False:
            result[key] = "not_offered"
            continue
        if key == "full" and config.product.mode == "full":
            result[key] = 1.0
            continue
        ratio = cost_ratio(shape_for(config, **kwargs), billing)
        result[key] = None if isinstance(ratio, Unknown) else round(ratio, 4)
    return result


def pricing(configs: list[ProviderConfig], day: dt.date) -> dict[str, Any]:
    """The declared prices of every provider, as the page draws them: the whole curve as
    [searches a month, price per 1,000 or None, index into plans], and at each step of the
    ladder the month's bill taken apart with its formula."""
    providers: dict[str, Any] = {}
    for config in configs:
        billing = config.billing_at(day)
        if billing is None:
            continue
        plans: list[str | None] = []
        points: list[list[Any]] = []
        bills: dict[str, list[Any]] = {}  # keyed by volume, as text: JSON has no other keys
        for point in price_curve(Shape(), billing):
            quote = point.quote
            if isinstance(quote, Unknown):
                points.append([point.monthly_volume, None, 0])
                continue
            if quote.plan_name not in plans:
                plans.append(quote.plan_name)
            points.append(
                [point.monthly_volume, round(quote.price_per_1k, 4), plans.index(quote.plan_name)]
            )
            if point.monthly_volume in LADDER_VOLUMES:
                bills[str(point.monthly_volume)] = [
                    round(quote.monthly_cost, 2),
                    quote.plan_price,
                    round(quote.extra_cost, 2),
                    quote.extra,
                    quote.formula,
                ]
        providers[config.id] = {
            "checked_at": billing.checked_at.isoformat(),
            "effective_from": billing.effective_from.isoformat(),
            "sources": billing.sources,
            "unit": billing.meter.unit_label or billing.meter.unit,
            "units_per_search": billing.request_cost.base,
            "per_search": per_search(config, billing),
            "failed_request_charged": billing.charges.failed_request.charged,
            "plans": plans or [None],
            "points": points,
            "bills": bills,
        }
    return {
        "date": day.isoformat(),
        "volumes": list(HEADLINE_VOLUMES),
        "ladder": list(LADDER_VOLUMES),
        "providers": providers,
    }


# --- the document --------------------------------------------------------------------------


def build_summary(
    records: list[Record],
    configs: list[ProviderConfig],
    *,
    now: dt.datetime,
    operator: str | None = None,
    repository: str | None = None,
    methodology_version: str | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    """The whole summary. `configs` are the providers to show, in the order to show them.

    A provider appears once it is `published`, and has at least one request that counts.
    With `preview` every measured provider appears whatever its status: that is for looking
    at the page before launch, and the page says so.

    The main figures use the headline pool only (desktop, first page), without warm-ups
    and control repeats. Step 3 is judged over all the records, because a page's verdict
    needs the other providers' pages of the same moment and the earlier runs.
    """
    verdicts = judge(records)
    counted = [r for r in records if r.headline and r.counts]
    known_days = [day for day in map(day_of, counted) if day is not None]
    last = max(known_days) if known_days else now.date()
    days = [last - dt.timedelta(days=offset) for offset in range(HISTORY_DAYS - 1, -1, -1)]

    by_provider: dict[str, list[Record]] = {}
    for record in counted:
        by_provider.setdefault(record.provider, []).append(record)

    shown = [
        config
        for config in configs
        if by_provider.get(config.id) and (preview or config.status == "published")
    ]
    # A run is placed in time by the searches that are counted: from the first of its
    # headline tasks to the last.
    runs = run_list(counted)
    providers = [
        _provider(config, by_provider[config.id], days, operator, verdicts, runs)
        for config in shown
    ]
    on_page = [record for config in shown for record in by_provider[config.id]]
    run_days = {run["start_at"][:10] for run in runs}
    return {
        "schema": SUMMARY_SCHEMA,
        "generated_at": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": last.isoformat(),
        "first_day": min(known_days).isoformat() if known_days else None,
        "days": [day.isoformat() for day in days],
        "windows": list(WINDOWS),
        "methodology_version": methodology_version,
        "repository": repository,
        "preview": preview,
        "not_measured_yet": list(NOT_MEASURED_YET),
        "poison": poison_stage(on_page, verdicts, last),
        "runs": runs,
        "coverage": {
            "first_at": runs[0]["start_at"] if runs else None,
            "last_at": runs[-1]["end_at"] if runs else None,
            "run_count": len(runs),
            "day_count": len(run_days),
            "timezone": "UTC",
        },
        "events": [],
        "providers": providers,
        "pricing": pricing(shown, now.date()),
    }
