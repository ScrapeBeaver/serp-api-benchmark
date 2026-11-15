"""The AI Overview block: what counts as its text, which searches had one, and the figure."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import ROOT

from serp_bench.ai_overview import MIN_CHARS, has_text, read, searches_with_one
from serp_bench.fixtures import dummy_env
from serp_bench.models import OUTCOME_DELIVERED, AiOverview, RawResponse, Record
from serp_bench.provider import load_spec
from serp_bench.summary import measure, per_search

TEXT = "Paella valenciana is a rice dish cooked in a wide pan over an open fire. " * 4
assert len(TEXT) > MIN_CHARS


# --- what counts as the overview's text ----------------------------------------------------


def test_no_block_is_not_an_empty_block():
    assert read(None) is None
    assert read({}) == AiOverview(chars=0)
    assert read([]) == AiOverview(chars=0)
    assert not has_text(read(None)) and not has_text(read([]))


def test_only_the_overviews_own_words_are_counted():
    """Sources, addresses, tokens and ids are not text: a block is not longer because it
    names more references."""
    plain = read({"text": TEXT})
    assert plain is not None and plain.chars == len(TEXT.strip()) and has_text(plain)
    with_references = read(
        {
            "text_blocks": [{"type": "paragraph", "snippet": TEXT}],
            "references": [
                {"title": "A very long title of a source " * 20, "link": "https://example.com/a"}
            ],
            "sources": ["another source " * 50],
            "thumbnail": "data:image/png;base64," + "A" * 500,
            "id": "x" * 300,
        }
    )
    assert with_references is not None
    assert with_references.chars == len(TEXT.strip())


def test_a_first_line_is_not_the_overview():
    teaser = read({"text_blocks": [{"snippet": "Paella is a rice dish from Valencia."}]})
    assert teaser is not None and 0 < teaser.chars < MIN_CHARS
    assert not has_text(teaser) and not teaser.deferred


def test_a_token_for_a_second_request_is_deferred_not_text():
    pointer = read({"page_token": "abc123", "serpapi_link": "https://serpapi.com/search.json?x=1"})
    assert pointer == AiOverview(chars=0, deferred=True)
    assert not has_text(pointer)
    # A block that has the text is not deferred, whatever else it carries.
    assert read({"text": TEXT, "page_token": "abc123"}) == AiOverview(chars=len(TEXT.strip()))


def test_a_block_that_only_says_it_is_there_is_empty():
    assert read({"position": 8, "ai_is_loading": False}) == AiOverview(chars=0)


# --- every adapter points at its provider's block --------------------------------------------


def _answer(provider: str) -> tuple[object, dict]:
    spec = load_spec(ROOT / "providers" / provider)
    fixture = json.loads((spec.fixtures_dir / "success.json").read_text(encoding="utf-8"))
    return spec.create(dummy_env(spec)), fixture["response"]["body"]


def _parse(adapter, body: dict):
    return adapter.parse(RawResponse(status=200, body=json.dumps(body).encode()))


PLACES = {
    "scrapebeaver": lambda body, block: body.__setitem__("ai_overview", block),
    "scrapedo": lambda body, block: body.__setitem__("ai_overview", block),
    "serpapi": lambda body, block: body.__setitem__("ai_overview", block),
    "scrapingdog": lambda body, block: body.__setitem__("ai_overview", block),
    "brightdata": lambda body, block: body.__setitem__("ai_overview", block),
    "scrapingbee": lambda body, block: body.__setitem__("ai_overviews", [block]),
    "scraperapi": lambda body, block: body.__setitem__("ai_box", block),
    "oxylabs": lambda body, block: body["results"][0]["json"]["results"].__setitem__(
        "ai_overviews", [block]
    ),
    "dataforseo": lambda body, block: body["tasks"][0]["result"][0]["items"].append(
        {"type": "ai_overview", **block}
    ),
}


@pytest.mark.parametrize("provider", sorted(PLACES))
def test_the_adapter_finds_the_block_where_its_provider_puts_it(provider):
    adapter, body = _answer(provider)
    assert _parse(adapter, body).ai_overview is None, "the recorded answer has no overview"
    PLACES[provider](body, {"text": TEXT})
    found = _parse(adapter, body).ai_overview
    assert found is not None and has_text(found) and found.chars >= len(TEXT.strip())


@pytest.mark.parametrize("provider", ["decodo", "serper"])
def test_a_provider_with_no_block_in_this_product_reports_none(provider):
    adapter, body = _answer(provider)
    body["ai_overview"] = {"text": TEXT}  # not a key these answers have: nothing is read
    assert _parse(adapter, body).ai_overview is None


# --- which searches had one, and the figure ----------------------------------------------------


def record(provider: str, task: str, overview: AiOverview | None, **changes) -> Record:
    base = {
        "run_id": "run-1",
        "config_hash": "c",
        "methodology_version": "0.1",
        "pool": "core",
        "headline": True,
        "task_id": task,
        "query_id": task,
        "query": task,
        "country": "US",
        "locale": "en",
        "device": "desktop",
        "page": 1,
        "provider": provider,
        "provider_status": "published",
        "adapter_version": "1",
        "scheduled_at": "2026-11-15T10:00:00.000Z",
        "started_at": "2026-11-15T10:00:00.000Z",
        "dispatch_lag_ms": 0.0,
        "ttfb_ms": 900.0,
        "total_ms": 1000.0,
        "http_status": 200,
        "bytes": 1000,
        "outcome": OUTCOME_DELIVERED,
        "ai_overview": overview,
    }
    return Record(**{**base, **changes})


FULL = AiOverview(chars=1800)
TEASER = AiOverview(chars=120)
TOKEN = AiOverview(chars=0, deferred=True)


def test_a_search_had_an_overview_when_two_providers_returned_its_text():
    records = [
        record("a", "t1", FULL),
        record("b", "t1", FULL),
        record("c", "t1", None),
        record("a", "t2", FULL),  # alone: one provider cannot vouch for it
        record("b", "t2", TEASER),
        record("a", "t3", FULL),
        record("b", "t3", FULL, warmup=True),  # a warm-up is not looked at
        record("a", "t4", FULL),
        record("b", "t4", FULL, outcome="timeout"),  # nor is an answer that failed
    ]
    assert searches_with_one(records) == {("run-1", "t1")}


def test_the_figure_is_the_share_of_those_searches_answered_with_the_text():
    records = [
        *(record("a", f"t{i}", FULL) for i in range(4)),
        *(record("b", f"t{i}", FULL) for i in range(4)),
        record("c", "t0", FULL),
        record("c", "t1", TEASER),
        record("c", "t2", TOKEN),
        record("c", "t3", None),
        record("c", "t9", FULL),  # nobody else returned one here: not counted either way
        record(
            "d", "t0", FULL, outcome="timeout"
        ),  # a failed answer is step 1's loss, not this one's
    ]
    overviews = searches_with_one(records)
    assert len(overviews) == 4

    def figures(provider):
        return measure([r for r in records if r.provider == provider], None, overviews)

    assert figures("a")["ai_overview"][3:] == [4, 4]
    c = figures("c")
    assert c["ai_overview"][3:] == [1, 4] and c["ai_overview"][0] == 25.0
    assert c["ai_overview_chars"] == [1800, 1]
    assert figures("d")["ai_overview"] is None  # no delivered answer to a search that had one
    assert measure(records)["ai_overview"] is None  # not asked for: no figure, not zero


# --- what a search with an overview costs, as declared ------------------------------------------


def test_the_declared_cost_of_a_search_with_an_overview():
    day = dt.date(2026, 11, 15)

    def ratio(provider):
        config = load_spec(ROOT / "providers" / provider).config
        return per_search(config, config.billing_at(day))["ai_overview"]

    assert ratio("scrapedo") == 1.5  # 10 credits, and 5 for the second call
    assert ratio("serpapi") == 2.0  # the second request counts as a search
    assert ratio("decodo") == "not_offered"  # the light target does not return it
    # Nothing prices it and nothing says it is free: not stated, never "no charge".
    assert ratio("serper") is None and ratio("brightdata") is None
