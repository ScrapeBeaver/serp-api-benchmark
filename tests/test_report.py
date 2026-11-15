"""The report is computed from records alone, and every figure can be checked by hand."""

from __future__ import annotations

import pytest

from serp_bench.metrics import CHECKS, METHODS, METRICS, to_markdown
from serp_bench.models import Device, Record
from serp_bench.report import (
    Quantile,
    Share,
    render,
    report_data,
    summarise,
)


def record(
    outcome: str = "delivered",
    total_ms: float | None = 1000.0,
    *,
    provider: str = "alpha",
    pool: str = "core",
    headline: bool = True,
    **more: object,
) -> Record:
    values: dict[str, object] = {
        "run_id": "run-1",
        "config_hash": "cfg",
        "methodology_version": "0.1",
        "pool": pool,
        "headline": headline,
        "task_id": f"t-{id(more)}",
        "query_id": "q",
        "query": "q",
        "country": "US",
        "locale": "en",
        "device": Device.desktop,
        "page": 1,
        "provider": provider,
        "provider_status": "published",
        "adapter_version": "1",
        "scheduled_at": None,
        "started_at": None,
        "dispatch_lag_ms": 1.0,
        "ttfb_ms": None,
        "total_ms": total_ms,
        "http_status": 200,
        "bytes": 100,
        "outcome": outcome,
    }
    values.update(more)
    return Record(**values)  # type: ignore[arg-type]


def records_for(
    delivered_ms: list[float], failures: dict[str, int], **more: object
) -> list[Record]:
    out = [record(total_ms=ms, **more) for ms in delivered_ms]
    for name, count in failures.items():
        out += [record(name, None, **more) for _ in range(count)]
    return out


def test_counts_and_shares():
    rows = records_for([1000.0] * 90, {"timeout": 6, "http_5xx": 4})
    rows += [record("void", None, void_reason="auth_or_credits") for _ in range(3)]
    rows += [record("void", None, void_reason="not_supported") for _ in range(2)]
    rows += [record(warmup=True)]
    report = summarise(rows)[0]
    assert report.tasks == 100
    assert dict(report.void) == {"auth_or_credits": 3, "not_supported": 2}
    assert (report.delivered.k, report.delivered.n) == (90, 100)
    assert {name: share.k for name, share in report.failures.items()} == {
        "http_5xx": 4,
        "timeout": 6,
    }
    assert report.delivered.k + sum(s.k for s in report.failures.values()) == report.tasks


def test_a_share_carries_its_wilson_interval():
    share = Share(90, 100)
    assert share.interval == pytest.approx((0.8256, 0.9448), abs=5e-4)
    assert share.text() == "90/100 = 90.0% [82.6%, 94.5%]"
    assert Share(0, 0).text() == "n/a (0 tasks)"


def test_deadline_shares_are_over_all_tasks():
    rows = records_for([2000.0] * 50 + [7000.0] * 30 + [20000.0] * 10, {"timeout": 10})
    report = summarise(rows)[0]
    assert {t: (s.k, s.n) for t, s in report.within.items()} == {
        5: (50, 100),
        10: (80, 100),
        30: (90, 100),
    }


def test_two_latency_views():
    rows = records_for([float(ms) for ms in range(1000, 1090)], {"timeout": 10})  # 90 of 100
    few = {q.p: q for q in summarise(rows[:40])[0].latency_delivered}
    assert few[50].state == "ok"  # 40 values: enough for a median, not for the tail
    assert few[95].state == "too_few" and few[95].text() == "n/a (n=40, needs 50)"
    report = summarise(rows)[0]
    delivered = {q.p: q for q in report.latency_delivered}
    overall = {q.p: q for q in report.time_to_answer}

    # Delivered only: 90 values, enough for every percentile (the tail ones need 50).
    assert delivered[50].state == "ok" and delivered[50].value_ms == pytest.approx(1044.5)
    assert delivered[90].state == "ok" and delivered[90].value_ms == pytest.approx(1080.1)
    assert delivered[95].state == "ok" and delivered[95].value_ms == pytest.approx(1084.55)

    # All tasks: 100 values, ten of them never answered. The view is the inverse of the
    # empirical distribution: p50 is the 50th shortest wait, p90 the 90th. Exactly 90% were
    # delivered, so p90 IS reached (it is the slowest delivered answer) and p95 is not.
    assert overall[50].state == "ok" and overall[50].value_ms == pytest.approx(1049.0)
    assert overall[90].state == "ok" and overall[90].value_ms == pytest.approx(1089.0)
    assert overall[95].state == "not_reached" and overall[95].text() == "not reached (n=100)"
    assert overall[99].state == "not_reached"  # 100 values are enough; 99% never answered
    assert overall[50].value_ms > delivered[50].value_ms, "failures push every quantile out"


def test_a_quantile_needs_enough_values():
    assert Quantile(50, 19, None).state == "too_few"
    assert Quantile(50, 20, 1234.0).text() == "1.23 s (n=20)"
    report = summarise(records_for([1000.0] * 19, {}))[0]
    assert all(q.state == "too_few" for q in report.latency_delivered)


def test_late_dispatch_is_counted_not_hidden():
    rows = records_for([1000.0] * 8, {}) + [record(client_lag=True, dispatch_lag_ms=900.0)] * 2
    report = summarise(rows)[0]
    assert (report.client_lag, report.tasks) == (2, 10)
    assert report.delivered.k == 10, "a late request still counts"


def test_units_reported_are_summed_where_stated():
    rows = (
        [record(billed_units=1.0)] * 3 + [record("http_5xx", None, billed_units=0.0)] + [record()]
    )
    report = summarise(rows)[0]
    assert (report.units_reported, report.units_stated) == (3.0, 4)


def test_rendering_shows_fractions_intervals_and_definitions():
    rows = records_for([1500.0] * 45, {"timeout": 5})
    text = render(rows)
    assert "45/50 = 90.0% [78.6%, 95.7%]" in text
    assert "Failed: timeout" in text and "5/50 = 10.0%" in text
    assert "n/a (n=45, needs 50)" in text
    assert "Wilson 95% score interval" in text
    assert "assume independent requests" in text
    assert "Hyndman and Fan's type 7" in text
    for metric in METRICS:
        definition = metric.definition.replace("T seconds", "5 s")
        assert definition in text, metric.id


def test_rendering_marks_providers_that_are_not_published():
    rows = records_for([1000.0] * 5, {}, provider="beta", provider_status="shadow")
    rows += records_for([1000.0] * 5, {}, provider="alpha")
    text = render(rows)
    assert "beta (shadow, not published)" in text
    assert "alpha (" not in text


def test_the_headline_leaves_other_pools_out():
    rows = records_for([1000.0] * 30, {}) + records_for(
        [1000.0] * 4, {"timeout": 6}, pool="mobile", headline=False
    )
    data = report_data(rows)
    assert data["headline"][0]["delivered"] == {
        "k": 30,
        "n": 30,
        "share": 1.0,
        "wilson95": pytest.approx([0.8865, 1.0], abs=5e-4),
    }
    assert data["by_pool"]["mobile"][0]["delivered"]["k"] == 4
    assert "mobile (not in the headline; desktop, page 1)" in render(rows)


def test_a_pool_nobody_could_run_reads_not_supported():
    rows = [record("void", None, void_reason="not_supported", pool="mobile", headline=False)] * 3
    assert "not supported" in render(rows + records_for([1000.0] * 3, {}))


def test_mixed_configurations_are_called_out():
    rows = records_for([1000.0] * 3, {}) + [record(config_hash="other")]
    assert "more than one configuration" in render(rows)


def test_markdown_rendering_is_a_table():
    text = render(records_for([1000.0] * 25, {}), markdown=True)
    assert "| Metric | Value | How it is computed |" in text
    assert "### Headline pools (core)" in text


def test_nothing_to_report():
    assert render([record(warmup=True)]) == "No records to report."


def test_the_registry_is_complete_and_printable():
    ids = [m.id for m in METRICS]
    assert len(set(ids)) == len(ids)
    markdown = to_markdown()
    for item in (*CHECKS, *METRICS, *METHODS):
        assert item.definition in markdown
    assert markdown.index("`dispatched`") < markdown.index("`params_echo`")
