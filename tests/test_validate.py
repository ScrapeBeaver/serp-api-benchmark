"""URL handling and the step-1 judge: every rule, and the trail it leaves."""

from __future__ import annotations

import pytest
from conftest import FakeProvider, fake_config, make_task, ok_body, raw

from serp_bench.metrics import CHECKS
from serp_bench.models import FailureClass, RawResponse
from serp_bench.validate import (
    host_of,
    is_absolute_http_url,
    is_google_host,
    is_google_wrapper,
    judge,
    normalise_url,
    url_hash,
)


@pytest.fixture
def provider(specs):
    return FakeProvider(fake_config(specs))


# --- URLs -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://WWW.Example.com/Path/", "https://example.com/Path"),
        ("http://example.com:80/a#section", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/?utm_source=x&id=7&gclid=abc", "https://example.com?id=7"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
        ("https://example.com/page#:~:text=highlight", "https://example.com/page"),
        ("https://example.com", "https://example.com"),
    ],
)
def test_normalise_url(url, expected):
    assert normalise_url(url) == expected


def test_url_hash_is_stable_and_ignores_noise():
    a = url_hash("https://www.example.com/page/?utm_campaign=x")
    assert a == url_hash("https://example.com/page") and len(a) == 16
    assert a != url_hash("https://example.com/other")


def test_host_of():
    assert host_of("https://WWW.Example.co.uk/x") == "example.co.uk"
    assert host_of("https://news.example.com/x") == "news.example.com"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("google.com", True),
        ("www.google.com", True),
        ("google.co.uk", True),
        ("maps.google.de", True),
        ("google.com.br", True),
        ("googleblog.com", False),
        ("notgoogle.com", False),
        ("example.com", False),
    ],
)
def test_is_google_host(host, expected):
    assert is_google_host(host) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a", True),
        ("http://example.com", True),
        ("/goto?url=abc", False),
        ("example.com/a", False),
        ("javascript:void(0)", False),
        ("", False),
    ],
)
def test_is_absolute_http_url(url, expected):
    assert is_absolute_http_url(url) is expected


# --- the judge --------------------------------------------------------------------------------


def outcome_of(provider, response, **task):
    return judge(provider, make_task(**task), response, timeout=60)


def test_delivered_passes_every_check_in_registry_order(provider):
    step = outcome_of(provider, raw(200, ok_body()))
    assert step.outcome == "delivered"
    assert [c.check for c in step.checks] == [c.id for c in CHECKS]
    assert all(c.passed for c in step.checks)
    assert step.organic_count == 10 and len(step.results) == 10
    assert step.results[0].host == "site1.example" and not step.results[0].on_google


@pytest.mark.parametrize(
    ("response", "outcome", "failed_check"),
    [
        (RawResponse(error="timeout", total_ms=60000), "timeout", "response_received"),
        (RawResponse(error="network", error_detail="ConnectError"), "network", "response_received"),
        (raw(429, {"e": 1}), "http_429", "http_2xx"),
        (raw(404, {"e": 1}), "http_4xx", "http_2xx"),
        (raw(503, {"e": 1}), "http_5xx", "http_2xx"),
        (raw(302, {}), "provider_error", "http_2xx"),
        (raw(200, text="<html>oops</html>"), "invalid_json", "json_body"),
        (raw(200, {"unexpected": True}), "malformed_results", "parsed"),
        (raw(200, ok_body(count=0)), "empty_results", "has_results"),
        (raw(200, ok_body(count=2)), "empty_results", "min_organic"),
        (raw(200, ok_body(query="coffee")), "param_mismatch", "params_echo"),
        (raw(200, ok_body(country="US")), "param_mismatch", "params_echo"),
    ],
)
def test_each_failure_class(provider, response, outcome, failed_check):
    step = outcome_of(provider, response)
    assert step.outcome == outcome
    assert outcome in {f.value for f in FailureClass}
    last = step.checks[-1]
    assert (last.check, last.passed) == (failed_check, False)
    assert all(c.passed for c in step.checks[:-1]), "the trail stops at the first failure"
    assert last.detail


def test_three_results_is_enough(provider):
    assert outcome_of(provider, raw(200, ok_body(count=3))).outcome == "delivered"


@pytest.mark.parametrize("field", ["title", "url"])
def test_one_broken_result_is_kept_and_marked_but_does_not_fail_the_page(provider, field):
    body = ok_body()
    body["results"][4][field] = "  " if field == "title" else "/goto?url=opaque"
    step = outcome_of(provider, raw(200, body))
    assert step.outcome == "delivered"
    assert len(step.results) == 10, "the broken result stays in the record"
    assert [ref.well_formed for ref in step.results].count(False) == 1
    assert not step.results[4].well_formed
    assert "9 of 10" in next(c.detail for c in step.checks if c.check == "well_formed_results")
    if field == "url":
        assert step.results[4].wrapper and step.results[4].url_hash is None
        assert step.results[4].host == ""


def test_a_page_with_fewer_than_three_usable_results_is_malformed(provider):
    body = ok_body()
    for result in body["results"][2:]:
        result["url"] = "/goto?url=opaque"
    step = outcome_of(provider, raw(200, body))
    assert step.outcome == "malformed_results"
    assert step.checks[-1].check == "well_formed_results"
    assert "only 2 of 10" in step.checks[-1].detail
    assert sum(ref.wrapper for ref in step.results) == 8


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.google.com/goto?url=CAESZQ", True),
        ("https://google.co.uk/url?q=https://example.com", True),
        ("/goto?url=CAESZQ", True),
        ("/url?q=https://example.com", True),
        ("https://www.google.com/search?q=coffee", True),
        ("https://www.google.com/sorry/index", True),
        ("https://share.google/AbCdEf", True),
        ("https://support.google.com/mail/answer/8494", False),  # a real page on a Google site
        ("https://play.google.com/store/apps/details?id=x", False),
        ("https://www.google.com/maps/place/Cafe", False),
        ("https://example.com/goto?url=x", False),
        ("/about", False),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_google_wrapper(url, expected):
    assert is_google_wrapper(url) is expected


def test_an_adapter_that_cannot_read_a_body_costs_one_request_not_the_run(provider, monkeypatch):
    def explode(_raw):
        raise IndexError("list index out of range")

    monkeypatch.setattr(provider, "parse", explode)
    step = outcome_of(provider, raw(200, ok_body()))
    assert step.outcome == "malformed_results" and step.checks[-1].check == "parsed"
    assert "IndexError" in step.checks[-1].detail


def test_echo_comparison_ignores_case_and_spacing(provider):
    body = ok_body(query="  Coffee  SHOPS london ", country="gb")
    assert outcome_of(provider, raw(200, body)).outcome == "delivered"


def test_a_provider_that_does_not_echo_passes(provider):
    body = ok_body()
    del body["q"], body["gl"]
    step = outcome_of(provider, raw(200, body))
    assert step.outcome == "delivered"
    assert "does not say" in step.checks[-1].detail


def test_links_to_google_are_flagged_but_step_one_does_not_judge_them(provider):
    body = ok_body()
    body["results"][0]["url"] = "https://www.google.com/goto?url=CAESZQ"
    step = outcome_of(provider, raw(200, body))
    assert step.outcome == "delivered"
    assert step.results[0].on_google and step.results[0].host == "google.com"
    assert step.results[0].wrapper, "a /goto link is an unresolved redirect"
    assert not any(ref.on_google or ref.wrapper for ref in step.results[1:])


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_credit_statuses_are_void(provider, status):
    step = outcome_of(provider, raw(status, {"detail": "no"}))
    assert (step.outcome, step.void_reason) == ("void", "auth_or_credits")
    assert step.checks[-1].check == "account_ok" and not step.checks[-1].passed


def test_an_error_reported_in_a_2xx_body(specs):
    class Reporting(FakeProvider):
        def classify_error(self, raw):
            return FailureClass.provider_error if raw.json().get("error") else None

    body = ok_body() | {"error": "captcha"}
    step = outcome_of(Reporting(fake_config(specs)), raw(200, body))
    assert step.outcome == "provider_error"
    assert step.checks[-1].check == "no_provider_error"
    assert "captcha" not in step.checks[-1].detail, "nothing from the body in a detail"


def test_the_adapter_can_declare_a_response_our_fault(specs):
    class Quota(FakeProvider):
        def own_fault(self, raw):
            return "auth_or_credits" if raw.status == 429 else None

    step = outcome_of(Quota(fake_config(specs)), raw(429, {"limit": "monthly"}))
    assert (step.outcome, step.void_reason) == ("void", "auth_or_credits")


def test_details_carry_numbers_people_can_check(provider):
    step = outcome_of(provider, raw(200, ok_body(count=2)))
    assert step.checks[-1].detail == "2 organic results, need at least 3"
    step = judge(provider, make_task(), RawResponse(error="timeout"), timeout=60)
    assert step.checks[-1].detail == "no complete response within 60 s"
