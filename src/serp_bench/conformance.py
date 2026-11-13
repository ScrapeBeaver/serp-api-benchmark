"""Conformance: call a provider for real and check the adapter and the declaration.

A fixed set of probes, one request each. For every probe the report says whether the
answer was what the probe expects, and whether the units the response says were charged
match what `provider.yaml` declares for that request shape.

Verdicts: PASS, FAIL, UNKNOWN (cannot be told from this response) and SKIPPED (the provider
declares it does not offer this). Conformance passes when nothing FAILs.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from serp_bench.models import (
    OUTCOME_DELIVERED,
    OUTCOME_VOID,
    Device,
    FailureClass,
    Record,
    Task,
)
from serp_bench.pricing import Shape, Unknown, billing_at, units_for
from serp_bench.provider import Provider
from serp_bench.runner import RunContext, build_record, client_for, send
from serp_bench.validate import judge

CONFORMANCE_SCHEMA_VERSION = 1
MAX_AGE_DAYS = 45

PASS, FAIL, UNKNOWN, SKIPPED = "PASS", "FAIL", "UNKNOWN", "SKIPPED"


@dataclass(frozen=True)
class Probe:
    id: str
    description: str
    query: str
    capability: str | None = None  # the declared capability this probe exercises
    device: Device = Device.desktop
    page: int = 1
    location: str | None = None
    operators: bool = False
    expect_empty: bool = False

    def task(self) -> Task:
        return Task(
            task_id=f"conformance-{self.id}",
            pool="conformance",
            headline=False,
            query_id=self.id,
            query=self.query,
            country="US",
            locale="en",
            device=self.device,
            page=self.page,
            location=self.location,
        )

    def shape(self) -> Shape:
        return Shape(
            device=self.device.value,  # type: ignore[arg-type]
            page=self.page,
            location="city" if self.location else "country",
            query_operators=self.operators,
        )


PROBES: tuple[Probe, ...] = (
    Probe("desktop_page1", "An ordinary desktop search, first page", "best running shoes"),
    Probe(
        "mobile",
        "The same search as a phone",
        "best running shoes",
        capability="mobile",
        device=Device.mobile,
    ),
    Probe(
        "page2", "The second results page", "best running shoes", capability="pagination", page=2
    ),
    Probe(
        "city_location",
        "A search from a place below country level",
        "coffee shops",
        capability="city_location",
        location="Austin,Texas,United States",
    ),
    Probe(
        "search_operators",
        "A query with a search operator",
        "site:wikipedia.org coffee",
        operators=True,
    ),
    Probe(
        "no_results",
        "A quoted nonsense phrase: nothing matches it, so the page is empty or substituted",
        '"zqxjvkpw qhzvnnrt wxkqjzpf"',
        expect_empty=True,
    ),
)


@dataclass
class ProbeResult:
    probe: Probe
    declared: Any = None  # the declared capability value, when the probe has one
    record: Record | None = None  # None when the probe was skipped
    response_verdict: str = SKIPPED
    response_detail: str = ""
    billing_verdict: str = SKIPPED
    billing_detail: str = ""
    units_measured: float | None = None
    units_declared: float | None = None

    def as_dict(self) -> dict[str, Any]:
        record = self.record
        return {
            "id": self.probe.id,
            "description": self.probe.description,
            "capability": self.probe.capability,
            "declared": self.declared,
            "sent": record is not None,
            "outcome": record.outcome if record else None,
            "void_reason": record.void_reason if record else None,
            "http_status": record.http_status if record else None,
            "ttfb_ms": record.ttfb_ms if record else None,
            "total_ms": record.total_ms if record else None,
            "organic_count": record.organic_count if record else None,
            "checks": [c.model_dump() for c in record.checks] if record else [],
            "response": {"verdict": self.response_verdict, "detail": self.response_detail},
            "billing": {
                "verdict": self.billing_verdict,
                "units_measured": self.units_measured,
                "units_declared": self.units_declared,
                "detail": self.billing_detail,
            },
        }


@dataclass
class ConformanceReport:
    provider: str
    provider_status: str
    adapter_version: str
    date: str
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def overall(self) -> str:
        verdicts = [p.response_verdict for p in self.probes] + [
            p.billing_verdict for p in self.probes
        ]
        if FAIL in verdicts:
            return FAIL
        responses = [p.response_verdict for p in self.probes if p.response_verdict != SKIPPED]
        return PASS if responses and all(v == PASS for v in responses) else UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFORMANCE_SCHEMA_VERSION,
            "provider": self.provider,
            "provider_status": self.provider_status,
            "adapter_version": self.adapter_version,
            "date": self.date,
            "overall": self.overall,
            "probes": [probe.as_dict() for probe in self.probes],
        }


def _judge_response(probe: Probe, declared: Any, record: Record) -> tuple[str, str]:
    outcome = record.outcome
    last = record.checks[-1].detail if record.checks else ""
    if outcome == OUTCOME_VOID:
        return UNKNOWN, f"void ({record.void_reason}): our own key or credit; fix it and rerun"
    if probe.expect_empty:
        if outcome == FailureClass.empty_results:
            return PASS, "answered with an empty results page"
        if outcome == OUTCOME_DELIVERED:
            # Google itself, finding nothing for a quoted phrase, shows results for the words
            # without the quotes. A provider that passes them on has copied the page
            # faithfully, so this is not a failure of delivery. It is a finding: the results
            # are not for the query asked, and it matters whether the response says so.
            if record.substituted:
                return PASS, (
                    f"returned {record.organic_count} results and says they are substitutes: "
                    "Google found nothing for the phrase as asked"
                )
            return UNKNOWN, (
                f"returned {record.organic_count} results for a phrase with no matches: these "
                "are Google's substitutes for the words without quotes, and the response does "
                "not say so; a finding"
            )
        return UNKNOWN, f"an empty search was answered with '{outcome}' ({last}); a finding"
    if outcome == OUTCOME_DELIVERED:
        note = "; declared unknown: set the capability to true" if declared == "unknown" else ""
        return PASS, f"delivered, {record.organic_count} results{note}"
    if declared == "unknown":
        return UNKNOWN, f"'{outcome}' ({last}); unsupported or a failure, cannot be told apart"
    return FAIL, f"'{outcome}' ({last})"


def _judge_billing(probe: Probe, provider: Provider, record: Record, day: dt.date) -> ProbeResult:
    """Compare what the response says was charged with what the YAML declares."""
    result = ProbeResult(probe)
    result.units_measured = record.billed_units
    if record.outcome == OUTCOME_VOID:
        result.billing_verdict, result.billing_detail = UNKNOWN, "void request"
        return result
    billing = billing_at(provider.config, day)
    if isinstance(billing, Unknown):
        result.billing_verdict, result.billing_detail = UNKNOWN, billing.reason
        return result

    declared_units = units_for(probe.shape(), billing)
    if record.outcome == OUTCOME_DELIVERED:
        charged: bool | str = True
        what = "a delivered request"
    elif record.outcome == FailureClass.empty_results:
        charged, what = billing.charges.empty_serp.charged, "an empty results page"
    elif record.outcome == FailureClass.timeout:
        charged, what = billing.charges.timeout.charged, "a timeout"
    else:
        charged, what = billing.charges.failed_request.charged, "a failed request"

    if charged is False:
        expected: float | Unknown = 0.0
    elif charged is True:
        expected = declared_units
    else:
        expected = Unknown(f"the provider does not document whether {what} is charged")

    if isinstance(expected, Unknown):
        result.billing_verdict = UNKNOWN
        result.billing_detail = expected.reason
        if record.billed_units is not None:
            result.billing_detail += f"; the response says {record.billed_units:g}"
        return result
    result.units_declared = expected
    if record.billed_units is None:
        result.billing_verdict = UNKNOWN
        result.billing_detail = f"declared {expected:g}; the response does not say what it charged"
        return result
    same = abs(record.billed_units - expected) < 1e-9
    result.billing_verdict = PASS if same else FAIL
    result.billing_detail = (
        f"declared {expected:g} for {what}, the response says {record.billed_units:g}"
    )
    return result


async def run_conformance(provider: Provider, *, timeout: float = 60.0) -> ConformanceReport:
    """Send every applicable probe, one at a time, and judge the answers."""
    now = dt.datetime.now(dt.UTC)
    report = ConformanceReport(
        provider=provider.id,
        provider_status=provider.config.status,
        adapter_version=provider.config.adapter.version,
        date=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    ctx = RunContext(f"conformance-{now:%Y%m%dT%H%M%S}", "conformance", "conformance")
    client = client_for(provider, timeout, 1)
    try:
        for probe in PROBES:
            declared = (
                getattr(provider.config.capabilities, probe.capability)
                if probe.capability
                else None
            )
            if declared is False:
                skipped = ProbeResult(probe, declared=False)
                skipped.response_detail = "declared as not offered; not sent"
                skipped.billing_detail = "not sent"
                report.probes.append(skipped)
                continue
            task = probe.task()
            started_at = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
            raw = await send(client, provider.build_request(task), timeout)
            step = judge(provider, task, raw, timeout=timeout)
            record = build_record(
                ctx, task, provider, step, raw=raw, started_at=started_at.replace("+00:00", "Z")
            )
            result = _judge_billing(probe, provider, record, now.date())
            result.declared = declared
            result.record = record
            result.response_verdict, result.response_detail = _judge_response(
                probe, declared, record
            )
            report.probes.append(result)
    finally:
        await client.aclose()
    return report


def render(report: ConformanceReport, *, markdown: bool = False) -> str:
    headers = ["Probe", "Outcome", "Time", "Response", "Billing", "Detail"]
    rows = []
    for item in report.probes:
        record = item.record
        time_text = f"{record.total_ms / 1000:.2f} s" if record and record.total_ms else "n/a"
        detail = item.response_detail
        if item.billing_detail and item.billing_verdict not in (SKIPPED,):
            detail += f" | billing: {item.billing_detail}"
        rows.append(
            [
                item.probe.id,
                record.outcome if record else "not sent",
                time_text,
                item.response_verdict,
                item.billing_verdict,
                detail,
            ]
        )
    if markdown:
        lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        lines += ["| " + " | ".join(row).replace("\n", " ") + " |" for row in rows]
        table = "\n".join(lines)
    else:
        widths = [max(len(c) for c in column) for column in zip(headers, *rows, strict=True)]
        lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
        lines.append("  ".join("-" * w for w in widths))
        lines += [
            "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
        ]
        table = "\n".join(lines)
    title = (
        f"Conformance of {report.provider} (adapter {report.adapter_version}, "
        f"status {report.provider_status}) on {report.date}: {report.overall}"
    )
    return f"{title}\n\n{table}"


def write(report: ConformanceReport, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "conformance.json"
    path.write_text(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")
    return path


def age_in_days(folder: Path, today: dt.date) -> int | None:
    """Days since the conformance run stored in `folder`, or None when there is none."""
    path = folder / "conformance.json"
    if not path.is_file():
        return None
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))["date"]
        return (today - dt.date.fromisoformat(stamp[:10])).days
    except (ValueError, KeyError, TypeError):
        return None
