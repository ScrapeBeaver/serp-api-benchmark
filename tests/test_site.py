"""The summary the page is built from, and the page itself."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import ROOT

from serp_bench import cli
from serp_bench.models import OUTCOME_DELIVERED, OUTCOME_VOID
from serp_bench.site import HEAD_MARK, PAGES, SUMMARY_MARK, build_site, render
from serp_bench.summary import (
    HISTORY_DAYS,
    NOT_MEASURED_YET,
    WINDOWS,
    build_summary,
    latency,
    link_counts,
    load_records,
    measure,
    resolved,
)

CONFIG = ["--config", str(ROOT / "bench.yaml")]
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)
TEMPLATE = ROOT / "site" / "template.html"


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    """A run of the offline mock: deliveries and failures, with no network."""
    out = tmp_path_factory.mktemp("run")
    args = ["run", *CONFIG, "--providers", "mock", "--run", "0", "--limit", "40"]
    assert cli.main([*args, "--interval", "0.01", "--out", str(out)]) == 0
    return load_records(out / "raw")


@pytest.fixture(scope="module")
def summary(records, specs):
    return build_summary(
        records,
        [specs["mock"].config],
        now=NOW,
        operator="mock",
        repository="https://github.com/example/benchmark",
        methodology_version="0.1",
        preview=True,
    )


# --- the figures -----------------------------------------------------------------------------


def test_the_shares_are_counted_from_the_records(records):
    counted = [r for r in records if r.headline and not r.warmup]
    figures = measure(counted)
    valid = [r for r in counted if r.outcome != OUTCOME_VOID]
    delivered = [r for r in valid if r.outcome == OUTCOME_DELIVERED]
    assert figures["tasks"] == len(valid) and figures["void"] == len(counted) - len(valid)
    percent, low, high, successes, n = figures["success"]
    assert (successes, n) == (len(delivered), len(valid))
    assert percent == pytest.approx(100 * successes / n, abs=0.01)
    assert low <= percent <= high
    # Step 2 is judged on delivered pages only: by links, and by whole pages.
    assert figures["resolved"][4] == sum(len(r.results) for r in delivered)
    assert figures["resolved_pages"][4] == len(delivered)


def test_what_is_not_measured_yet_is_empty_not_zero(records):
    figures = measure(records)
    assert all(figures[key] is None for key in NOT_MEASURED_YET)


def test_a_percentile_needs_enough_answers(records):
    delivered = [r for r in records if r.outcome == OUTCOME_DELIVERED]
    assert measure(delivered[:19])["p50"] is None  # 20 are needed for a median
    assert measure(delivered[:5])["p95"] is None  # and 50 for a 95th percentile


def test_one_link_back_to_google_makes_a_page_unresolved(records):
    page = next(r for r in records if r.outcome == OUTCOME_DELIVERED and r.results)
    assert resolved(page)
    unresolved_link = page.results[0].model_copy(update={"wrapper": True})
    assert not resolved(page.model_copy(update={"results": [unresolved_link, *page.results[1:]]}))


def test_resolved_is_the_share_of_links_and_the_whole_pages_are_counted_next_to_it(records):
    """One address missing on one page costs that one link in the headline figure, and the
    whole page in the figure that decides which pages go on to step 3."""
    pages = [r for r in records if r.outcome == OUTCOME_DELIVERED and len(r.results) >= 3][:4]
    assert len(pages) == 4 and all(resolved(p) for p in pages)
    links = sum(len(p.results) for p in pages)
    assert link_counts(pages) == (links, links)
    broken = pages[0].results[0].model_copy(update={"wrapper": True})
    pages[0] = pages[0].model_copy(update={"results": [broken, *pages[0].results[1:]]})
    assert link_counts(pages) == (links - 1, links)
    figures = measure(pages)
    assert figures["resolved"][3:] == [links - 1, links]
    assert figures["resolved"][0] == pytest.approx(100 * (links - 1) / links, abs=0.01)
    assert figures["resolved_pages"][3:] == [3, 4]


def test_the_summary_has_every_window_and_a_day_by_day_history(summary):
    assert summary["windows"] == list(WINDOWS) and len(summary["days"]) == HISTORY_DAYS
    (provider,) = summary["providers"]
    assert provider["self"] is True and provider["id"] == "mock"
    assert set(provider["windows"]) == {str(n) for n in WINDOWS}
    assert all(len(series) == HISTORY_DAYS for series in provider["daily"].values())
    assert provider["daily"]["success"][-1] == provider["windows"]["1"]["success"][0]
    outcomes = provider["outcomes"]
    assert outcomes["resolved"] + outcomes["unresolved"] + outcomes["failed"] == outcomes["tasks"]
    step3 = outcomes["clean"] + outcomes["poisoned"] + outcomes["unjudged"]
    assert step3 == outcomes["resolved"]
    assert "mock" in summary["pricing"]["providers"]


def test_only_published_providers_are_shown_outside_a_preview(records, specs):
    kwargs = {"now": NOW, "operator": None}
    assert specs["mock"].config.status != "published"
    assert (
        build_summary(records, [specs["mock"].config], preview=False, **kwargs)["providers"] == []
    )
    published = specs["mock"].config.model_copy(update={"status": "published"})
    shown = build_summary(records, [published], preview=False, **kwargs)["providers"]
    assert [p["id"] for p in shown] == ["mock"]


def test_a_provider_with_no_records_is_not_on_the_page(specs):
    assert build_summary([], [specs["mock"].config], now=NOW, preview=True)["providers"] == []


# --- the pages -------------------------------------------------------------------------------

URL = "https://benchmark.example.com"


def page_named(key):
    return next(page for page in PAGES if page.key == key)


def built(summary, key):
    return render(TEMPLATE.read_text(encoding="utf-8"), page_named(key), summary, URL)


def test_the_template_has_its_marks_once():
    template = TEMPLATE.read_text(encoding="utf-8")
    for mark in (SUMMARY_MARK, HEAD_MARK, "{{PAGE}}", "{{H1}}", '<table id="results"></table>'):
        assert template.count(mark) == 1, mark
    assert template.count("<!-- /panel -->") == len(PAGES)
    assert "noindex" not in template  # indexing is decided at build time, from the summary


def test_each_page_carries_one_section_and_its_own_words(summary):
    pages = {page.key: built(summary, page.key) for page in PAGES}
    for key, text in pages.items():
        assert text.count("data-panel=") == 1 and f'data-panel="{key}"' in text
        assert f'data-panel="{key}" hidden' not in text  # the page's own section is shown
        assert f'data-tab="{key}" aria-current="page"' in text
        assert "{{" not in text and "__HEAD__" not in text
    titles = {text.split("<title>")[1].split("</title>")[0] for text in pages.values()}
    headings = {text.split("<h1>")[1].split("</h1>")[0] for text in pages.values()}
    descriptions = {
        text.split('name="description" content="')[1].split('"')[0] for text in pages.values()
    }
    assert len(titles) == len(headings) == len(descriptions) == len(PAGES)
    assert all(title.endswith("| ScrapeBeaver") for title in titles)
    assert f'<link rel="canonical" href="{URL}/pricing/">' in pages["pricing"]
    # A page one level down reaches the root with "../".
    assert (
        'href="../methodology/"' in pages["pricing"] and 'href="./pricing/"' in pages["performance"]
    )


def test_the_page_carries_the_data_and_reads_without_a_script(summary):
    page = built(summary, "performance")
    start = page.index("const SUMMARY = ") + len("const SUMMARY = ")
    assert json.loads(page[start : page.index(";\n", start)]) == summary
    # The main table is already in the HTML, with the figures, for a reader with no script.
    success = summary["providers"][0]["windows"]["30"]["success"][0]
    table = page.split('<table id="results">')[1].split("</table>")[0]
    assert summary["providers"][0]["name"] in table and f"{success:.1f}%" in table
    prices = built(summary, "pricing").split('id="volumes">')[1].split("</table>")[0]
    assert "<caption>Price per 1,000 searches" in prices and "10,000" in prices


def test_structured_data_says_what_each_page_is(summary):
    def kinds(key):
        text = built(summary, key)
        raw = text.split('<script type="application/ld+json">')[1].split("</script>")[0]
        return [thing["@type"] for thing in json.loads(raw)["@graph"]]

    assert kinds("performance") == ["Organization", "WebSite", "Dataset"]
    assert kinds("pricing") == ["Organization", "WebSite", "BreadcrumbList", "Dataset"]
    assert kinds("methodology") == ["Organization", "WebSite", "BreadcrumbList", "TechArticle"]


def test_a_preview_asks_not_to_be_indexed_and_a_launched_site_asks_to_be(summary, tmp_path):
    build_site(summary, TEMPLATE, tmp_path / "preview", URL)
    assert (tmp_path / "preview" / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    for page in PAGES:
        text = (tmp_path / "preview" / page.path / "index.html").read_text()
        assert 'content="noindex, nofollow"' in text

    launched = summary | {"preview": False}
    written = build_site(launched, TEMPLATE, tmp_path / "live", URL)
    names = {str(p.relative_to(tmp_path / "live")) for p in written}
    assert names >= {
        "index.html", "pricing/index.html", "methodology/index.html", "summary.json",
        "llms.txt", "robots.txt", "sitemap.xml", "CNAME", "assets/og.png", "assets/icon.svg",
    }  # fmt: skip
    robots = (tmp_path / "live" / "robots.txt").read_text()
    assert f"Sitemap: {URL}/sitemap.xml" in robots and "Disallow" not in robots
    assert "User-agent: GPTBot" in robots and "User-agent: ClaudeBot" in robots
    sitemap = (tmp_path / "live" / "sitemap.xml").read_text()
    assert [f"{URL}/{page.path}" in sitemap for page in PAGES] == [True] * len(PAGES)
    assert 'content="index, follow' in (tmp_path / "live" / "index.html").read_text()
    assert (tmp_path / "live" / "CNAME").read_text() == "benchmark.example.com\n"
    assert json.loads((tmp_path / "live" / "summary.json").read_text()) == launched


def test_llms_txt_states_the_figures_in_plain_text(summary, tmp_path):
    build_site(summary, TEMPLATE, tmp_path, URL)
    text = (tmp_path / "llms.txt").read_text()
    window = summary["providers"][0]["windows"]["30"]
    assert f"success rate {window['success'][0]:.1f}%" in text
    assert f"Based on {window['tasks']} searches" in text
    assert "Not measured yet: usable answers." in text
    assert "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)" in text
    assert ", poisoned results " in text
    assert all(f"{URL}/{page.path}" in text for page in PAGES)


def test_text_from_the_data_cannot_break_out_of_the_page(summary):
    hostile = json.loads(json.dumps(summary))
    hostile["providers"][0]["name"] = '</script><script>alert(1)</script><b title="x">'
    for page in PAGES:
        text = built(hostile, page.key)
        assert "</script><script>alert(1)" not in text
    table = built(hostile, "performance").split('<table id="results">')[1].split("</table>")[0]
    assert "&lt;/script&gt;" in table


def test_the_site_command_builds_pages_even_before_any_results(tmp_path, capsys):
    (tmp_path / "raw").mkdir()
    out = tmp_path / "d"
    code = cli.main(["site", *CONFIG, "--records", str(tmp_path / "raw"), "--out", str(out)])
    assert code == 0
    assert "none yet" in capsys.readouterr().out
    assert '"providers":[]' in (out / "index.html").read_text()
    # The methodology reads even when there is nothing to show yet.
    assert "Step 2 · Are the URLs resolved?" in (out / "methodology" / "index.html").read_text()


# --- what the results page on the main site reads --------------------------------------------


def test_the_summary_lists_its_runs_and_what_they_cover(summary, records):
    counted = [r for r in records if r.headline and r.counts]
    assert [run["id"] for run in summary["runs"]] == [counted[0].run_id]
    run = summary["runs"][0]
    assert run["start_at"] <= run["end_at"] and run["start_at"].endswith("Z")
    assert summary["coverage"] == {
        "first_at": run["start_at"],
        "last_at": run["end_at"],
        "run_count": 1,
        "day_count": 1,
        "timezone": "UTC",
    }


def test_each_provider_has_its_figures_run_by_run(summary):
    provider = summary["providers"][0]
    assert len(provider["runs"]) == len(summary["runs"])
    one = provider["runs"][0]
    assert set(one) == {
        "tasks",
        "success",
        "resolved",
        "resolved_pages",
        "poisoned",
        "p50",
        "p95",
    }
    # One run in the window: the run's figures are the window's.
    window = provider["windows"]["30"]
    assert all(one[key] == window[key] for key in one)


def test_response_times_are_given_so_that_failures_count_against_a_deadline(records):
    counted = [r for r in records if r.headline and r.counts]
    times = latency(counted)
    valid = [r for r in counted if r.outcome != OUTCOME_VOID]
    delivered = [r for r in valid if r.outcome == OUTCOME_DELIVERED]
    assert (times["tasks"], times["delivered"], times["exact"]) == (
        len(valid),
        len(delivered),
        True,
    )
    assert times["seconds"] == sorted(times["seconds"]) and len(times["seconds"]) == len(delivered)
    # The share of all searches answered within T: the times under T, out of every search.
    assert len(delivered) < len(valid), "the mock fails some searches"
    assert sum(s <= 60 for s in times["seconds"]) / times["tasks"] < 1


def test_a_long_history_gives_quantiles_instead_of_every_time(records, monkeypatch):
    monkeypatch.setattr("serp_bench.summary.LATENCY_EXACT_MAX", 5)
    counted = [r for r in records if r.headline and r.counts]
    times = latency(counted)
    exact = sorted(r.total_ms / 1000 for r in counted if r.outcome == OUTCOME_DELIVERED)
    assert times["exact"] is False and len(times["seconds"]) == 201
    assert times["seconds"][0] == round(exact[0], 3) and times["seconds"][-1] == round(exact[-1], 3)
    assert times["seconds"] == sorted(times["seconds"]) and times["delivered"] == len(exact)
