"""The report of a run, computed from records and nothing else.

Every share is printed as numerator/denominator with its interval, and every line carries
the one-line definition of the metric, taken from the registry in `metrics.py`. A run is
a small sample: the report describes what happened in it and does not rank anyone.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from serp_bench.metrics import method, metric
from serp_bench.models import (
    OUTCOME_DELIVERED,
    OUTCOME_VOID,
    Record,
)
from serp_bench.stats import MIN_N_FOR_QUANTILE, quantile, quantile_ecdf, wilson_interval

DEADLINES_SECONDS = (5, 10, 30)
QUANTILES = (50, 90, 95, 99)


@dataclass(frozen=True)
class Share:
    """k of n, with its Wilson 95% interval."""

    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.k, self.n)

    def text(self) -> str:
        if self.n == 0:
            return "n/a (0 tasks)"
        low, high = self.interval  # type: ignore[misc]
        return f"{self.k}/{self.n} = {self.k / self.n:.1%} [{low:.1%}, {high:.1%}]"

    def as_dict(self) -> dict[str, Any]:
        interval = self.interval
        return {
            "k": self.k,
            "n": self.n,
            "share": self.value,
            "wilson95": list(interval) if interval else None,
        }


@dataclass(frozen=True)
class Quantile:
    """One percentile, or the reason it is not shown."""

    p: int
    n: int  # values behind it
    value_ms: float | None  # None when not shown; math.inf when not reached

    @property
    def needed(self) -> int:
        return MIN_N_FOR_QUANTILE[self.p]

    @property
    def state(self) -> str:
        if self.n < self.needed:
            return "too_few"
        if self.value_ms is not None and math.isinf(self.value_ms):
            return "not_reached"
        return "ok"

    def text(self) -> str:
        if self.state == "too_few":
            return f"n/a (n={self.n}, needs {self.needed})"
        if self.state == "not_reached":
            return f"not reached (n={self.n})"
        return f"{self.value_ms / 1000:.2f} s (n={self.n})"  # type: ignore[operator]

    def as_dict(self) -> dict[str, Any]:
        shown = self.value_ms if self.state == "ok" else None
        return {"p": self.p, "n": self.n, "state": self.state, "ms": shown}


def _quantiles(values: Sequence[float], method=quantile) -> list[Quantile]:
    return [
        Quantile(
            p, len(values), method(values, p) if len(values) >= MIN_N_FOR_QUANTILE[p] else None
        )
        for p in QUANTILES
    ]


@dataclass
class ProviderReport:
    provider: str
    status: str = "published"
    tasks: int = 0
    void: Counter[str] = field(default_factory=Counter)  # by reason
    client_lag: int = 0
    delivered: Share = Share(0, 0)
    failures: dict[str, Share] = field(default_factory=dict)
    within: dict[int, Share] = field(default_factory=dict)
    latency_delivered: list[Quantile] = field(default_factory=list)
    time_to_answer: list[Quantile] = field(default_factory=list)
    units_reported: float = 0.0
    units_stated: int = 0  # responses that said what they charged

    @property
    def void_total(self) -> int:
        return sum(self.void.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "tasks": self.tasks,
            "void": dict(self.void),
            "client_lag": self.client_lag,
            "delivered": self.delivered.as_dict(),
            "failures": {name: share.as_dict() for name, share in self.failures.items()},
            "delivered_within": {str(t): s.as_dict() for t, s in self.within.items()},
            "latency_delivered": [q.as_dict() for q in self.latency_delivered],
            "time_to_answer": [q.as_dict() for q in self.time_to_answer],
            "units_reported": {"sum": self.units_reported, "responses": self.units_stated},
        }


def summarise(records: Iterable[Record]) -> list[ProviderReport]:
    """One report per provider over the given records. Warm-ups and control repeats are
    never counted."""
    by_provider: dict[str, list[Record]] = {}
    for record in records:
        if record.counts:
            by_provider.setdefault(record.provider, []).append(record)

    reports = []
    for provider in sorted(by_provider):
        rows = by_provider[provider]
        report = ProviderReport(provider, status=rows[-1].provider_status)
        delivered_ms: list[float] = []
        all_ms: list[float] = []
        failures: Counter[str] = Counter()
        for record in rows:
            if record.billed_units is not None:
                report.units_reported += record.billed_units
                report.units_stated += 1
            if record.outcome == OUTCOME_VOID:
                report.void[record.void_reason or "unknown"] += 1
                continue
            report.tasks += 1
            report.client_lag += record.client_lag
            if record.outcome == OUTCOME_DELIVERED and record.total_ms is not None:
                delivered_ms.append(record.total_ms)
                all_ms.append(record.total_ms)
            else:
                all_ms.append(math.inf)
                if record.outcome != OUTCOME_DELIVERED:
                    failures[record.outcome] += 1
        n = report.tasks
        report.delivered = Share(len(delivered_ms), n)
        report.failures = {name: Share(count, n) for name, count in sorted(failures.items())}
        report.within = {
            deadline: Share(sum(1 for ms in delivered_ms if ms <= deadline * 1000), n)
            for deadline in DEADLINES_SECONDS
        }
        report.latency_delivered = _quantiles(delivered_ms)
        report.time_to_answer = _quantiles(all_ms, quantile_ecdf)
        reports.append(report)
    return reports


def report_data(records: Sequence[Record]) -> dict[str, Any]:
    """The report as plain data, for the JSON log and for anyone who wants the numbers."""
    counted = [record for record in records if record.counts]
    headline = [record for record in counted if record.headline]
    pools = sorted({record.pool for record in counted})
    return {
        "run_id": counted[0].run_id if counted else None,
        "config_hashes": sorted({record.config_hash for record in counted}),
        "headline": [report.as_dict() for report in summarise(headline)],
        "by_pool": {
            pool: [r.as_dict() for r in summarise(x for x in counted if x.pool == pool)]
            for pool in pools
        },
    }


# --- rendering ------------------------------------------------------------------------------

_STATUS_NOTE = {
    "shadow": "shadow, not published",
    "conformance": "conformance, not published",
    "draft": "draft, not published",
}


def _label(report: ProviderReport) -> str:
    note = _STATUS_NOTE.get(report.status)
    return f"{report.provider} ({note})" if note else report.provider


def _metric_lines(report: ProviderReport) -> list[tuple[str, str, str]]:
    """(name, value, definition) for every metric of one provider."""
    lines = [(metric("tasks").name, str(report.tasks), metric("tasks").definition)]
    void = ", ".join(f"{reason} {count}" for reason, count in sorted(report.void.items()))
    lines.append(
        (metric("void").name, f"{report.void_total}" + (f" ({void})" if void else ""),
         metric("void").definition)
    )  # fmt: skip
    lag = Share(report.client_lag, report.tasks)
    lines.append((metric("client_lag").name, lag.text(), metric("client_lag").definition))
    lines.append(
        (metric("delivered").name, report.delivered.text(), metric("delivered").definition)
    )
    for name, share in report.failures.items():
        lines.append((f"Failed: {name}", share.text(), metric("failures").definition))
    for deadline, share in report.within.items():
        definition = metric("delivered_within").definition.replace("T seconds", f"{deadline} s")
        lines.append((f"Delivered within {deadline} s", share.text(), definition))
    for item in report.latency_delivered:
        lines.append(
            (
                f"Latency p{item.p}, delivered only",
                item.text(),
                metric("latency_delivered").definition,
            )
        )
    for item in report.time_to_answer:
        lines.append(
            (
                f"Time to answer p{item.p}, all tasks",
                item.text(),
                metric("time_to_answer").definition,
            )
        )
    units = (
        f"{report.units_reported:g} over {report.units_stated} responses that state it"
        if report.units_stated
        else "not stated by the responses"
    )
    lines.append((metric("units_reported").name, units, metric("units_reported").definition))
    # A metric that takes several lines (quantiles, deadlines' siblings) is defined once.
    seen: set[str] = set()
    compact = []
    for name, value, definition in lines:
        compact.append((name, value, "" if definition in seen else definition))
        seen.add(definition)
    return compact


_SUMMARY_HEADERS = [
    "Provider",
    "Tasks",
    "Void",
    "Delivered",
    "Delivered within 10 s",
    "p50 delivered",
]


def _summary_rows(reports: Sequence[ProviderReport]) -> list[list[str]]:
    rows = []
    for report in reports:
        if report.tasks == 0 and set(report.void) == {"not_supported"}:
            rows.append(
                [_label(report), "0", str(report.void_total), "not supported", "n/a", "n/a"]
            )
            continue
        p50 = next(q for q in report.latency_delivered if q.p == 50)
        rows.append(
            [
                _label(report),
                str(report.tasks),
                str(report.void_total),
                report.delivered.text(),
                report.within[10].text(),
                p50.text(),
            ]
        )
    return rows


def _text_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def render(records: Sequence[Record], *, markdown: bool = False) -> str:
    """The report as plain text, or as Markdown for a CI job summary."""
    counted = [record for record in records if record.counts]
    if not counted:
        return "No records to report."
    table = _markdown_table if markdown else _text_table
    heading = (lambda text: f"### {text}") if markdown else (lambda text: f"== {text} ==")

    first = counted[0]
    configs = sorted({record.config_hash for record in counted})
    out = [
        f"Run {first.run_id}, methodology {first.methodology_version}, "
        f"configuration {', '.join(configs)}",
    ]
    if len(configs) > 1:
        out.append("WARNING: these records come from more than one configuration.")
    out += [
        "",
        "How to read this: " + method("first_attempt").definition,
        method("wilson").definition,
        method("quantile").definition + " " + method("min_n").definition,
        "",
    ]

    headline = [record for record in counted if record.headline]
    if headline:
        pools = ", ".join(sorted({record.pool for record in headline}))
        reports = summarise(headline)
        out += [
            heading(f"Headline pools ({pools})"),
            "",
            table(_SUMMARY_HEADERS, _summary_rows(reports)),
        ]
        for report in reports:
            out += ["", heading(_label(report)) if markdown else f"{_label(report)}:", ""]
            lines = _metric_lines(report)
            if markdown:
                out.append(_markdown_table(["Metric", "Value", "How it is computed"], lines))
            else:
                name_width = max(len(name) for name, _, _ in lines)
                value_width = max(len(value) for _, value, _ in lines)
                out += [
                    (
                        f"  {name.ljust(name_width)}  {value.ljust(value_width)}"
                        + (f"  | {definition}" if definition else "")
                    ).rstrip()
                    for name, value, definition in lines
                ]
    else:
        out.append("No headline pool was part of this run.")

    out += ["", heading("By pool"), ""]
    for pool in sorted({record.pool for record in counted}):
        in_pool = [record for record in counted if record.pool == pool]
        sample = in_pool[0]
        label = "headline" if sample.headline else "not in the headline"
        out.append(f"{pool} ({label}; {sample.device.value}, page {sample.page})")
        out.append("")
        out.append(table(_SUMMARY_HEADERS, _summary_rows(summarise(in_pool))))
        out.append("")
    return "\n".join(out).rstrip()
