"""Repeatability, and the features that not every provider offers: what the summary says
about them, and the pieces a record carries for it."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import FakeProvider, fake_config, make_task

from serp_bench.models import OUTCOME_DELIVERED, Device, OrganicResult, Record, ResultRef, Serp
from serp_bench.poison import OUTSIDE_GROUP, control_candidates, judge
from serp_bench.summary import (
    REPEATABILITY_MIN_PAIRS,
    build_summary,
    feature_figures,
    repeatability,
    same_results,
)
from serp_bench.validate import place_named, result_refs, title_hash

DAY = dt.datetime(2026, 11, 26, 2, 17, tzinfo=dt.UTC)
SITES = [f"site{i}.example" for i in range(10)]


def result(pos: int, site: str, *, wrapper: bool = False, title: str | None = None) -> ResultRef:
    return ResultRef(
        pos=pos,
        host="google.com" if wrapper else site,
        url_hash=None if wrapper else title_hash(f"https://{site}/"),
        on_google=wrapper,
        wrapper=wrapper,
        title_hash=title_hash(title or f"About {site}"),
    )


def record(
    sites: list[str] | None = None,
    *,
    task: str = "task-1",
    run: str = "run-1",
    provider: str = "a",
    query_id: str = "q-1",
    page: int = 1,
    headline: bool = True,
    outcome: str = OUTCOME_DELIVERED,
    wrappers: bool = False,
    **more: object,
) -> Record:
    sites = SITES if sites is None else sites
    results = [result(i + 1, site, wrapper=wrappers) for i, site in enumerate(sites)]
    stamp = DAY.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    values: dict[str, object] = {
        "run_id": run,
        "config_hash": "c",
        "methodology_version": "0.2",
        "pool": "core",
        "headline": headline,
        "task_id": task,
        "query_id": query_id,
        "query": "coffee shops",
        "country": "US",
        "locale": "en",
        "device": Device.desktop,
        "page": page,
        "provider": provider,
        "provider_status": "published",
        "adapter_version": "1",
        "scheduled_at": stamp,
        "started_at": stamp,
        "dispatch_lag_ms": 0.0,
        "ttfb_ms": 900.0,
        "total_ms": 1000.0,
        "http_status": 200,
        "bytes": 1000,
        "outcome": outcome,
        "organic_count": len(results),
        "results": results if outcome == OUTCOME_DELIVERED else [],
    }
    values.update(more)
    return Record(**values)  # type: ignore[arg-type]


def pair(number: int, again_sites: list[str] | None = None, *, page: int = 1, **more: object):
    """A search and its repeat, in the same run."""
    task = f"task-{number}-p{page}"
    first = record(task=task, page=page, **more)
    again = record(
        again_sites, task=f"{task}-again", page=page, headline=False, repeat_of=task, **more
    )
    return [first, again]


# --- what a record carries -------------------------------------------------------------------


def test_a_title_hash_ignores_case_and_spacing_and_is_not_the_title():
    assert title_hash("Best  Running Shoes ") == title_hash("best running shoes")
    assert title_hash("Best running shoes") != title_hash("Best running shoe")
    assert title_hash("") is None and title_hash("   ") is None
    assert "running" not in title_hash("Best running shoes")


def test_every_result_of_a_record_has_its_title_hash():
    serp = Serp(
        organic=[
            OrganicResult(position=1, url="https://a.example/x", title="One"),
            OrganicResult(position=2, url="/url?q=https://b.example", title="Two"),
        ]
    )
    refs = result_refs(serp)
    assert [ref.title_hash for ref in refs] == [title_hash("One"), title_hash("Two")]
    assert refs[1].wrapper and refs[1].url_hash is None  # the title is all that names it


def test_the_place_is_looked_for_in_the_address_the_title_and_the_snippet():
    serp = Serp(
        organic=[
            OrganicResult(position=1, url="https://a.example/new-york/dentists", title="Dentists"),
            OrganicResult(position=2, url="https://b.example", title="Dentist in New  York"),
            OrganicResult(position=3, url="https://c.example", title="C", snippet="Near NEW YORK."),
            OrganicResult(position=4, url="https://d.example", title="Dentists near you"),
        ]
    )
    assert place_named(serp, "New York,New York,United States") == 3
    assert place_named(serp, "Austin,Texas,United States") == 0


def test_only_the_first_ten_results_are_looked_at_for_the_place():
    organic = [
        OrganicResult(position=i, url=f"https://s{i}.example", title="Austin" if i > 10 else "x")
        for i in range(1, 13)
    ]
    assert place_named(Serp(organic=organic), "Austin,Texas,United States") == 0


def test_a_search_placed_somewhere_records_how_many_results_name_the_place(specs):
    from serp_bench.runner import RunContext, build_record
    from serp_bench.validate import judge as judge_request

    provider = FakeProvider(fake_config(specs))
    body = {
        "q": "dentist",
        "gl": "US",
        "results": [
            {"url": f"https://s{i}.example", "title": "Dentist in Austin" if i < 6 else "Dentist"}
            for i in range(9)
        ],
    }
    from conftest import raw

    placed = make_task(query="dentist", country="US", location="Austin,Texas,United States")
    step = judge_request(provider, placed, raw(body=body))
    assert step.outcome == OUTCOME_DELIVERED and step.place_named == 6
    kept = build_record(RunContext("r", "c", "0.2", 0), placed, provider, step, position=4)
    assert (kept.place_named, kept.location, kept.position) == (6, placed.location, 4)
    plain = judge_request(provider, make_task(query="dentist", country="US"), raw(body=body))
    assert plain.place_named is None  # no place asked for: nothing to count


# --- the same search twice ---------------------------------------------------------------------


def test_the_same_page_twice_is_one_and_two_unlike_pages_are_nought():
    first = record()
    assert same_results(first, record()) == 1.0
    assert same_results(first, record([f"other{i}.example" for i in range(10)])) == 0.0


def test_a_pair_is_scored_by_the_results_of_each_page_that_are_on_the_other():
    eight_shared = SITES[:8] + ["new1.example", "new2.example"]
    assert same_results(record(), record(eight_shared)) == pytest.approx(0.8)
    # the order of the results is not compared
    assert same_results(record(), record(list(reversed(SITES)))) == 1.0
    # a shorter second page: 6 of 10 and 6 of 6
    assert same_results(record(), record(SITES[:6])) == pytest.approx(12 / 16)


def test_wrapped_links_are_compared_by_title():
    """A wrapper's address says nothing, so two pages of wrappers are the same page when
    their titles are, and unlike when they are not."""
    assert same_results(record(wrappers=True), record(wrappers=True)) == 1.0
    other = record([f"other{i}.example" for i in range(10)], wrappers=True)
    assert same_results(record(wrappers=True), other) == 0.0
    # a provider that resolved the links the second time still returned the same results
    assert same_results(record(wrappers=True), record()) == 1.0


def test_a_page_without_results_cannot_be_compared():
    assert same_results(record(), record([])) is None


def test_repeatability_is_the_mean_of_first_pages_and_later_pages():
    half_new = SITES[:5] + [f"new{i}.example" for i in range(5)]
    records = []
    for number in range(REPEATABILITY_MIN_PAIRS):
        records += pair(number)  # first pages: the same page again
        records += pair(number, half_new, page=2 + number % 4)  # later pages: half of it
    figure = repeatability(records)
    assert figure["first"] == [100.0, REPEATABILITY_MIN_PAIRS]
    assert figure["later"] == [50.0, REPEATABILITY_MIN_PAIRS]
    assert figure["value"] == 75.0 and figure["min_pairs"] == REPEATABILITY_MIN_PAIRS


def test_repeatability_is_not_shown_until_each_half_has_enough_pairs():
    records = []
    for number in range(REPEATABILITY_MIN_PAIRS):
        records += pair(number)
    for number in range(REPEATABILITY_MIN_PAIRS - 1):
        records += pair(number, page=3)
    figure = repeatability(records)
    assert figure["value"] is None
    assert figure["first"][1] == REPEATABILITY_MIN_PAIRS
    assert figure["later"][1] == REPEATABILITY_MIN_PAIRS - 1
    assert repeatability([]) == {
        "value": None,
        "first": None,
        "later": None,
        "min_pairs": REPEATABILITY_MIN_PAIRS,
    }


def test_a_pair_counts_only_when_both_requests_were_delivered():
    failed_again = pair(1)
    failed_again[1] = record(task="task-1-p1-again", headline=False, repeat_of="task-1-p1",
                             outcome="timeout")  # fmt: skip
    failed_first = pair(2)
    failed_first[0] = record(task="task-2-p1", outcome="http_5xx")
    figure = repeatability([*pair(0), *failed_again, *failed_first])
    assert figure["first"] == [100.0, 1]


def test_a_repeat_is_paired_with_its_search_in_the_same_run_only():
    first, again = pair(0)
    elsewhere = again.model_copy(update={"run_id": "run-2"})
    assert repeatability([first, elsewhere])["first"] is None


# --- features ----------------------------------------------------------------------------------


def city(named: int | None, *, number: int = 0, results: int = 9, **more: object) -> Record:
    return record(
        SITES[:results],
        task=f"city-{number}",
        headline=False,
        feature="city",
        location="Austin,Texas,United States",
        place_named=named,
        **more,
    )


def test_a_city_page_is_one_where_half_of_the_first_ten_results_name_the_city():
    rows = [city(5, number=0, results=10), city(4, number=1, results=10), city(4, number=2,
            results=8), city(0, number=3)]  # fmt: skip
    figures = feature_figures("city", rows)
    assert figures["applied"][3:] == [2, 4]  # 5 of 10 and 4 of 8; not 4 of 10, not 0 of 9
    assert figures["success"][3:] == [4, 4] and figures["tasks"] == 4


def test_a_provider_that_ignores_the_place_delivers_every_answer_and_applies_none():
    figures = feature_figures("city", [city(0, number=n) for n in range(6)])
    assert figures["success"][0] == 100.0 and figures["applied"][0] == 0.0


def test_a_feature_search_that_was_not_sent_is_void_and_in_no_share():
    void = city(None, number=9, outcome="void", void_reason="not_supported")
    figures = feature_figures("city", [city(9, number=0), void])
    assert (figures["tasks"], figures["void"]) == (1, 1)
    assert figures["success"][3:] == [1, 1]
    nothing = feature_figures("city", [void])
    assert nothing["tasks"] == 0 and nothing["success"] is None and nothing["applied"] is None


def test_failures_of_a_feature_are_counted_by_class():
    rows = [city(9), city(None, number=1, outcome="empty_results"),
            city(None, number=2, outcome="empty_results")]  # fmt: skip
    figures = feature_figures("city", rows)
    assert figures["failures"] == {"empty_results": 2}
    assert figures["success"][3:] == [1, 3] and figures["applied"][3:] == [1, 1]


def week(sites: list[str], *, query_id: str = "q-1", **more: object) -> Record:
    return record(
        sites,
        task=f"week-{query_id}",
        query_id=query_id,
        headline=False,
        feature="week",
        time_range="week",
        **more,
    )


def test_a_filtered_page_is_read_against_the_same_search_without_the_filter():
    fresh = [f"fresh{i}.example" for i in range(10)]
    plain = record(task="core-1", query_id="q-1")
    assert feature_figures("week", [plain, week(fresh)])["applied"][3:] == [1, 1]
    # the same page as without the filter: the filter did nothing
    assert feature_figures("week", [plain, week(SITES)])["applied"][3:] == [0, 1]
    # exactly half the same still counts as filtered, one more does not
    half = SITES[:5] + fresh[:5]
    assert feature_figures("week", [plain, week(half)])["applied"][3:] == [1, 1]
    assert feature_figures("week", [plain, week(SITES[:6] + fresh[:4])])["applied"][3:] == [0, 1]


def test_a_filtered_page_with_nothing_to_read_it_against_is_not_judged():
    fresh = [f"fresh{i}.example" for i in range(10)]
    failed_plain = record(task="core-1", query_id="q-1", outcome="timeout")
    for rows in ([week(fresh)], [failed_plain, week(fresh)]):
        figures = feature_figures("week", rows)
        assert figures["applied"] is None and figures["success"][3:] == [1, 1]
    # the plain page of another run, or of another search, is not the one
    other_run = record(task="core-1", query_id="q-1", run="run-2")
    other_query = record(task="core-2", query_id="q-2")
    assert feature_figures("week", [other_run, other_query, week(fresh)])["applied"] is None


def test_the_figures_of_a_feature_are_the_main_tables_on_its_searches():
    rows = [city(9, number=n, wrappers=n == 0, total_ms=2000.0 + n) for n in range(20)]
    figures = feature_figures("city", [record(task="core-1"), *rows])
    assert figures["resolved"][3:] == [19 * 9, 20 * 9]  # links, as in the main table
    assert figures["p50"] == [pytest.approx(2.0095, abs=1e-3), 20]


# --- step 3 next to the features and the repeats ---------------------------------------------


def test_a_filtered_page_is_not_a_reference_for_the_search_without_the_filter():
    """History is kept per search, and a search for the past week is another search: its
    pages must never become the usual sites of the plain one."""
    from serp_bench.poison import _query_key

    plain = record()
    filtered = record(time_range="week")
    assert _query_key(plain) != _query_key(filtered)
    assert _query_key(plain) == _query_key(record(task="other", repeat_of="task-1"))


def test_only_searches_of_the_main_table_are_asked_for_a_control_repeat():
    odd = [f"odd{i}.example" for i in range(10)]

    def moment(task: str, **more: object) -> list[Record]:
        rows = [record(task=task, provider=p, **more) for p in ("a", "b", "c")]
        return [*rows, record(odd, task=task, provider="d", **more)]

    main = moment("task-1")
    feature = moment(
        "city-1", headline=False, feature="city", location="Austin,Texas,United States"
    )
    again = moment("task-1-again", headline=False, repeat_of="task-1")
    verdicts = judge([*main, *feature, *again])
    assert verdicts[("run-1", "city-1", "d")].agreement == OUTSIDE_GROUP
    wanted = control_candidates([*main, *feature, *again])
    assert [(r.task_id, r.provider) for r in wanted] == [("task-1", "d")]


# --- the summary -------------------------------------------------------------------------------


def test_the_summary_says_who_offers_each_feature_and_how_it_went(specs):
    config = specs["mock"].config
    fresh = [f"fresh{i}.example" for i in range(10)]
    records = [
        record(task="core-1", provider="mock"),
        week(fresh, provider="mock"),
        city(None, provider="mock", outcome="void", void_reason="not_supported"),
        *[r.model_copy(update={"provider": "mock"}) for r in pair(5)],
    ]
    summary = build_summary(records, [config], now=DAY, preview=True)
    assert summary["features"] == [
        {"id": "city", "name": "Search in a city", "measured": True},
        {"id": "week", "name": "Past week only", "measured": True},
        {"id": "web_tab", "name": "Web tab", "measured": False},
    ]
    provider = summary["providers"][0]
    features = provider["features"]
    assert features["city"]["offered"] is False  # the mock declares it does not
    assert features["city"]["windows"]["30"]["void"] == 1
    assert features["week"]["offered"] is True
    assert features["week"]["windows"]["30"]["applied"][3:] == [1, 1]
    assert features["web_tab"] == {"offered": False}  # declared only: no figures
    assert provider["repeatability"]["30"]["first"] == [100.0, 1]
    assert provider["repeatability"]["30"]["value"] is None
    # the main table is still the main table: two searches, not the feature or the repeat
    assert provider["windows"]["30"]["tasks"] == 2
