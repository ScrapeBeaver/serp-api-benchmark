"""Conformance: the probes, the verdicts, and the file it leaves."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
from conftest import FakeProvider, echo_body, fake_config

from serp_bench import conformance
from serp_bench.conformance import FAIL, PASS, PROBES, SKIPPED, UNKNOWN, run_conformance


def by_id(report):
    return {item.probe.id: item for item in report.probes}


def empty_for_quotes(request: httpx.Request) -> httpx.Response:
    body = echo_body(request)
    if body["q"].startswith('"'):
        body["results"] = []
    return httpx.Response(200, json=body)


class Billing(FakeProvider):
    """Says what it charged: one unit for any 200."""

    def billed_units(self, raw):
        return 1.0 if raw.status == 200 else 0.0


def test_the_probes_cover_the_declared_capabilities():
    assert [p.id for p in PROBES] == [
        "desktop_page1",
        "mobile",
        "page2",
        "city_location",
        "search_operators",
        "no_results",
    ]
    assert {p.capability for p in PROBES if p.capability} == {
        "mobile",
        "pagination",
        "city_location",
    }


def test_the_offline_mock_passes(specs):
    report = asyncio.run(run_conformance(specs["mock"].create({})))
    results = by_id(report)
    assert report.overall == PASS
    assert results["city_location"].response_verdict == SKIPPED  # declared false
    assert results["no_results"].record.outcome == "empty_results"
    for name in ("desktop_page1", "mobile", "page2", "search_operators", "no_results"):
        assert results[name].response_verdict == PASS, name
        assert results[name].billing_verdict == PASS, name


def test_a_capability_declared_true_that_fails_is_a_fail(specs):
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(500, json={})
            if b"best running shoes" in request.content
            else (empty_for_quotes(request))
        )

    provider = Billing(fake_config(specs, city_location=True), handler)
    report = asyncio.run(run_conformance(provider))
    results = by_id(report)
    assert results["desktop_page1"].response_verdict == FAIL
    assert "http_5xx" in results["desktop_page1"].response_detail
    assert results["city_location"].response_verdict == PASS
    assert report.overall == FAIL


def test_an_unknown_capability_is_probed_and_reported_as_found(specs):
    provider = Billing(
        fake_config(specs, mobile="unknown", city_location="unknown"), empty_for_quotes
    )
    results = by_id(asyncio.run(run_conformance(provider)))
    assert results["mobile"].response_verdict == PASS
    assert "set the capability to true" in results["mobile"].response_detail


def test_results_for_a_nonsense_phrase_and_an_error_are_findings(specs):
    always_results = Billing(fake_config(specs))  # answers ten results to anything
    results = by_id(asyncio.run(run_conformance(always_results)))
    # Google substitutes results for the words without quotes: a finding, not a failure.
    assert results["no_results"].response_verdict == UNKNOWN
    assert "substitutes" in results["no_results"].response_detail

    def error_for_quotes(request: httpx.Request) -> httpx.Response:
        if b'\\"zq' in request.content:
            return httpx.Response(502, json={})
        return httpx.Response(200, json=echo_body(request))

    results = by_id(asyncio.run(run_conformance(Billing(fake_config(specs), error_for_quotes))))
    assert results["no_results"].response_verdict == UNKNOWN
    assert "a finding" in results["no_results"].response_detail


def test_a_provider_that_says_the_results_are_substitutes_passes(specs):
    class Candid(Billing):
        """Answers anything with results, and says when they are not for the query asked."""

        def parse(self, raw):
            serp = super().parse(raw)
            return serp.model_copy(update={"substituted": "zqxjvkpw" in (serp.echo.query or "")})

    results = by_id(asyncio.run(run_conformance(Candid(fake_config(specs)))))
    assert results["no_results"].response_verdict == PASS
    assert "says they are substitutes" in results["no_results"].response_detail
    assert results["desktop_page1"].response_verdict == PASS


def test_charged_units_are_compared_with_the_declaration(specs):
    class Overcharging(FakeProvider):
        def billed_units(self, raw):
            return 2.0

    results = by_id(
        asyncio.run(run_conformance(Overcharging(fake_config(specs), empty_for_quotes)))
    )
    assert results["desktop_page1"].billing_verdict == FAIL
    assert results["desktop_page1"].units_declared == 1.0
    assert results["desktop_page1"].units_measured == 2.0
    assert "declared 1" in results["desktop_page1"].billing_detail

    silent = FakeProvider(fake_config(specs), empty_for_quotes)  # does not say what it charged
    report = asyncio.run(run_conformance(silent))
    assert by_id(report)["desktop_page1"].billing_verdict == UNKNOWN
    assert report.overall == PASS, "an unknown charge does not fail conformance"


def test_our_own_bad_key_is_unknown_not_a_fail(specs):
    provider = FakeProvider(fake_config(specs), lambda request: httpx.Response(401, json={}))
    report = asyncio.run(run_conformance(provider))
    assert {item.response_verdict for item in report.probes if item.record} == {UNKNOWN}
    assert report.overall == UNKNOWN


def test_the_written_file_has_no_secret_and_no_body(tmp_path, specs):
    provider = specs["scrapebeaver"].create({"SCRAPEBEAVER_API_KEY": "very-secret-key"})
    provider.transport = lambda: httpx.MockTransport(  # type: ignore[method-assign]
        lambda request: httpx.Response(401, json={"detail": "bad key very-secret-key"})
    )
    report = asyncio.run(run_conformance(provider))
    path = conformance.write(report, tmp_path)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "very-secret-key" not in text
    assert data["provider"] == "scrapebeaver" and data["adapter_version"] == "1"
    assert data["schema_version"] == 1 and len(data["probes"]) == len(PROBES)
    assert set(data["probes"][0]) >= {"id", "outcome", "checks", "response", "billing"}
    assert "results" not in data["probes"][0]
    assert conformance.render(report).startswith("Conformance of scrapebeaver")
    assert "| Probe |" in conformance.render(report, markdown=True)


def test_age_of_a_conformance_run(tmp_path):
    today = dt.date(2026, 11, 12)
    assert conformance.age_in_days(tmp_path, today) is None
    (tmp_path / "conformance.json").write_text('{"date": "2026-09-01T10:00:00Z"}', "utf-8")
    assert conformance.age_in_days(tmp_path, today) == 72
    (tmp_path / "conformance.json").write_text("not json", "utf-8")
    assert conformance.age_in_days(tmp_path, today) is None
