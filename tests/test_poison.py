"""Step 3, the substituted-page check, on pages written by hand."""

from __future__ import annotations

import datetime as dt

import pytest

from serp_bench import cli
from serp_bench.models import OUTCOME_DELIVERED, Device, Record, ResultRef
from serp_bench.poison import (
    CLEAN,
    DIFFERENT_AGAIN,
    DOUBTFUL,
    FAIL,
    IN_GROUP,
    NEAR_GROUP,
    NO_ANSWER,
    ONE_OFF,
    OUTSIDE_GROUP,
    PASS,
    PERSISTENT,
    POISONED,
    UNJUDGED,
    agree,
    clean_group,
    control_candidates,
    decide,
    judge,
    page_sites,
    site_of,
    tally,
    usual_sites,
)
from serp_bench.records import RecordWriter
from serp_bench.summary import measure, poison_stage

REAL = [f"real{i}.com" for i in range(10)]  # the sites Google really lists for the query
DAY = dt.datetime(2026, 11, 12, 10, 0, tzinfo=dt.UTC)


def fake(seed: str) -> list[str]:
    """Ten sites nobody else has: a substituted page."""
    return [f"{seed}{i}.net" for i in range(10)]


def page(
    provider: str,
    sites: list[str],
    *,
    run: str = "run-1",
    task: str = "task-1",
    when: dt.datetime = DAY,
    query: str = "personal loans online",
    number: int = 1,
    control: bool = False,
    outcome: str = OUTCOME_DELIVERED,
    wrappers: int = 0,
) -> Record:
    """One provider's page: `sites` in order, the first `wrappers` of them unresolved."""
    results = [
        ResultRef(
            pos=index + 1,
            host="google.com" if index < wrappers else site,
            url_hash=None if index < wrappers else f"{index:016x}",
            on_google=index < wrappers,
            wrapper=index < wrappers,
        )
        for index, site in enumerate(sites)
    ]
    stamp = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return Record(
        run_id=run,
        config_hash="c",
        methodology_version="0.1",
        pool="core",
        headline=True,
        task_id=task,
        query_id="q",
        query=query,
        country="US",
        locale="en",
        device=Device.desktop,
        page=number,
        provider=provider,
        provider_status="published",
        adapter_version="1",
        scheduled_at=stamp,
        started_at=stamp,
        dispatch_lag_ms=0.0,
        ttfb_ms=900.0,
        total_ms=1000.0,
        http_status=200,
        bytes=1000,
        outcome=outcome,
        organic_count=len(results),
        results=results if outcome == OUTCOME_DELIVERED else [],
        control=control,
    )


def verdict_of(records: list[Record], provider: str, run: str = "run-1", task: str = "task-1"):
    return judge(records)[(run, task, provider)]


def history(days: int = 4, providers: tuple[str, ...] = ("a", "b")) -> list[Record]:
    """Earlier runs of the same query, one a day, in which `providers` returned the real page."""
    records = []
    for back in range(1, days + 1):
        when = DAY - dt.timedelta(days=back)
        records += [page(p, REAL, run=f"old-{back}", when=when) for p in providers]
    return records


# --- the pieces --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "site"),
    [
        ("en.wikipedia.org", "wikipedia.org"),
        ("bbc.co.uk", "bbc.co.uk"),
        ("news.bbc.co.uk", "bbc.co.uk"),
        ("kakaku.com", "kakaku.com"),
        ("travel.rakuten.co.jp", "rakuten.co.jp"),
        ("g1.globo.com.br", "globo.com.br"),
        ("retty.me", "retty.me"),
        ("support.google.com", "google.com"),
    ],
)
def test_a_host_is_reduced_to_its_site(host, site):
    assert site_of(host) == site


def test_only_resolved_links_of_the_first_ten_are_compared():
    twelve = [*REAL, "eleventh.com", "twelfth.com"]
    assert page_sites(page("a", twelve)) == frozenset(REAL)
    # Three unresolved links: their google.com host is not a site of the page.
    assert page_sites(page("a", REAL, wrappers=3)) == frozenset(REAL[3:])
    # Six of ten unresolved leaves four: too few to say anything.
    assert page_sites(page("a", REAL, wrappers=6)) is None


def test_two_pages_agree_when_half_of_the_smaller_one_is_shared():
    ten = frozenset(REAL)
    assert agree(ten, frozenset([*REAL[:5], *fake("x")[:5]]))
    assert not agree(ten, frozenset([*REAL[:4], *fake("x")[:6]]))
    # An eight-result page with four shared sites agrees with a ten-result one.
    assert agree(ten, frozenset([*REAL[:4], *fake("x")[:4]]))
    assert not agree(ten, frozenset())


def test_the_group_is_not_a_majority_vote():
    """Two agree, three return three different pages: the two are the real ones."""
    pages = {
        "a": frozenset(REAL),
        "b": frozenset(REAL),
        "c": frozenset(fake("c")),
        "d": frozenset(fake("d")),
        "e": frozenset(fake("e")),
    }
    assert clean_group(pages) == {"a", "b"}


def test_no_group_when_nobody_agrees_or_two_groups_are_equal():
    assert clean_group({"a": frozenset(fake("a")), "b": frozenset(fake("b"))}) == frozenset()
    assert clean_group({"a": frozenset(REAL)}) == frozenset()
    two_pairs = {
        "a": frozenset(REAL),
        "b": frozenset(REAL),
        "c": frozenset(fake("x")),
        "d": frozenset(fake("x")),
    }
    assert clean_group(two_pairs) == frozenset()


def test_a_page_half_way_between_does_not_break_the_group():
    """a, b and c agree; d agrees with a and b but not with c. Both {a,b,c} and {a,b,d}
    are groups of three, so only a and b are certain."""
    half = [*REAL[:5], *fake("d")[:5]]
    pages = {
        "a": frozenset(REAL),
        "b": frozenset(REAL),
        "c": frozenset([*REAL[5:], *fake("c")[:5]]),
        "d": frozenset(half),
    }
    assert clean_group(pages) == {"a", "b"}


def test_usual_sites_are_those_on_most_reference_pages():
    reference = [frozenset(REAL)] * 3 + [frozenset([*REAL[:5], *fake("x")[:5]])] * 2
    # real0..4 are on 5 of 5 pages, real5..9 on 3 of 5 (60%), the x sites on 2 of 5.
    assert usual_sites(reference) == frozenset(REAL)
    assert usual_sites([]) == frozenset()


@pytest.mark.parametrize(
    ("history_says", "agreement_says", "expected"),
    [
        (PASS, IN_GROUP, CLEAN),
        (PASS, OUTSIDE_GROUP, CLEAN),  # passes A
        (PASS, None, CLEAN),
        (FAIL, IN_GROUP, CLEAN),  # everyone moved at once: Google changed
        (None, IN_GROUP, CLEAN),
        (DOUBTFUL, IN_GROUP, CLEAN),
        (FAIL, OUTSIDE_GROUP, POISONED),
        (FAIL, None, POISONED),
        (None, OUTSIDE_GROUP, POISONED),
        (None, None, UNJUDGED),  # no signal is available
        (DOUBTFUL, OUTSIDE_GROUP, UNJUDGED),
        (DOUBTFUL, None, UNJUDGED),
        (FAIL, NEAR_GROUP, UNJUDGED),
        (None, NEAR_GROUP, UNJUDGED),
    ],
)
def test_the_verdict_rule(history_says, agreement_says, expected):
    assert decide(history_says, agreement_says)[0] == expected


# --- whole runs ----------------------------------------------------------------------------


def test_the_page_that_matches_nobody_is_the_poisoned_one():
    records = [
        page("a", REAL),
        page("b", [*REAL[:8], "new1.com", "new2.com"]),
        page("c", fake("c")),
    ]
    assert verdict_of(records, "a").verdict == CLEAN
    assert verdict_of(records, "b").verdict == CLEAN
    odd = verdict_of(records, "c")
    assert odd.verdict == POISONED and odd.agreement == OUTSIDE_GROUP
    assert odd.group == ("a", "b") and odd.shared_with == {"a": (0, 10), "b": (0, 10)}
    assert odd.history is None and odd.signals == ("agreement",)
    assert tally(judge(records).values()) == {CLEAN: 2, POISONED: 1, UNJUDGED: 0}


def test_without_history_and_without_a_group_nothing_is_judged():
    records = [page("a", REAL), page("b", fake("b"))]
    for provider in ("a", "b"):
        verdict = verdict_of(records, provider)
        assert verdict.verdict == UNJUDGED and verdict.signals == ()


def test_a_page_near_the_group_is_left_unjudged():
    near = [*REAL[:5], *fake("d")[:5]]  # agrees with a and b, not with c
    records = [
        page("a", REAL),
        page("b", REAL),
        page("c", [*REAL[5:], *fake("c")[:5]]),
        page("d", near),
    ]
    assert verdict_of(records, "d").agreement == NEAR_GROUP
    assert verdict_of(records, "d").verdict == UNJUDGED


def test_a_page_with_unresolved_links_is_judged_on_the_resolved_ones():
    records = [page("a", REAL), page("b", REAL), page("c", REAL, wrappers=3)]
    assert verdict_of(records, "c").verdict == CLEAN
    records = [page("a", REAL), page("b", REAL), page("c", REAL, wrappers=7)]
    unjudged = verdict_of(records, "c")
    assert unjudged.verdict == UNJUDGED and "resolved links" in unjudged.reason


def test_failed_requests_warm_ups_and_controls_get_no_verdict():
    records = [
        page("a", REAL),
        page("b", REAL),
        page("c", [], outcome="timeout"),
        page("d", fake("d"), control=True),
    ]
    assert {key[2] for key in judge(records)} == {"a", "b"}


def test_each_moment_is_judged_on_its_own():
    """The same task id in two runs, and two tasks in one run, never mix."""
    records = [
        page("a", REAL, run="run-1"),
        page("b", REAL, run="run-1"),
        page("a", fake("x"), run="run-2"),
        page("b", fake("x"), run="run-2"),
        page("a", fake("y"), task="task-2"),
        page("b", fake("z"), task="task-2"),
    ]
    verdicts = judge(records)
    assert verdicts[("run-1", "task-1", "a")].verdict == CLEAN
    assert verdicts[("run-2", "task-1", "a")].verdict == CLEAN
    assert verdicts[("run-1", "task-2", "a")].verdict == UNJUDGED


# --- history ---------------------------------------------------------------------------------


def test_history_judges_a_page_when_no_provider_agrees_with_it():
    """Today only one provider answered. Its page is judged by the query's usual sites."""
    good = [*history(), page("a", [*REAL[:7], *fake("n")[:3]])]
    verdict = verdict_of(good, "a")
    assert (verdict.verdict, verdict.history, verdict.agreement) == (CLEAN, PASS, None)
    assert verdict.usual_share == 0.7 and verdict.reference_pages == 8

    bad = [*history(), page("a", [*REAL[:3], *fake("n")[:7]])]
    verdict = verdict_of(bad, "a")
    assert (verdict.verdict, verdict.history) == (POISONED, FAIL)

    doubtful = [*history(), page("a", [*REAL[:5], *fake("n")[:5]])]
    verdict = verdict_of(doubtful, "a")
    assert (verdict.verdict, verdict.history) == (UNJUDGED, DOUBTFUL)


def test_one_day_of_pages_is_not_a_history():
    same_day = [
        page(p, REAL, run=f"old-{hour}", when=DAY - dt.timedelta(hours=hour))
        for hour in (1, 2, 3)
        for p in ("a", "b")
    ]
    verdict = verdict_of([*same_day, page("a", fake("n"))], "a")
    assert verdict.history is None and verdict.verdict == UNJUDGED


def test_history_older_than_two_weeks_and_later_runs_are_not_used():
    old = [
        page(p, REAL, run=f"old-{back}", when=DAY - dt.timedelta(days=back))
        for back in (15, 16, 17, 18)
        for p in ("a", "b")
    ]
    assert verdict_of([*old, page("a", fake("n"))], "a").history is None
    later = [
        page(p, REAL, run=f"new-{ahead}", when=DAY + dt.timedelta(days=ahead))
        for ahead in (1, 2, 3, 4)
        for p in ("a", "b")
    ]
    assert verdict_of([*later, page("a", fake("n"))], "a").history is None


def test_only_pages_of_an_agreeing_group_become_history():
    """Four days on which a alone answered, with a substituted page each time: nothing of
    it is a reference, so there is no history to be wrong about."""
    alone = [
        page("a", fake("p"), run=f"old-{back}", when=DAY - dt.timedelta(days=back))
        for back in (1, 2, 3, 4)
    ]
    assert verdict_of([*alone, page("a", fake("p"))], "a").history is None


def test_history_is_kept_per_query_and_results_page():
    other_query = [*history(), page("a", fake("n"), query="car insurance quotes")]
    assert verdict_of(other_query, "a").history is None
    second_page = [*history(), page("a", fake("n"), number=2)]
    assert verdict_of(second_page, "a").history is None


def test_when_everyone_moves_at_once_google_changed():
    """History says these are not the usual sites, yet all providers return them."""
    moved = fake("update")
    records = [*history(), page("a", moved), page("b", moved), page("c", moved)]
    verdict = verdict_of(records, "c")
    assert (verdict.verdict, verdict.history, verdict.agreement) == (CLEAN, FAIL, IN_GROUP)
    assert "changed for everyone" in verdict.reason


# --- the control repeat ----------------------------------------------------------------------


def moment_with_control(control_page: Record) -> list[Record]:
    return [page("a", REAL), page("b", REAL), page("c", fake("c")), control_page]


def test_candidates_for_a_control_are_first_pages_outside_the_group():
    records = [
        page("a", REAL),
        page("b", REAL),
        page("c", fake("c")),
        page("a", REAL, task="task-2", number=2),
        page("b", REAL, task="task-2", number=2),
        page("c", fake("d"), task="task-2", number=2),
    ]
    assert [(r.provider, r.task_id) for r in control_candidates(records)] == [("c", "task-1")]


def test_a_control_that_matches_the_group_means_a_one_off():
    records = moment_with_control(page("c", REAL, control=True))
    verdict = verdict_of(records, "c")
    assert (verdict.verdict, verdict.control) == (POISONED, ONE_OFF)


def test_a_control_that_repeats_the_odd_page_means_it_is_persistent():
    records = moment_with_control(page("c", fake("c"), control=True))
    assert verdict_of(records, "c").control == PERSISTENT


def test_a_third_different_page_is_the_mark_of_substitution():
    records = moment_with_control(page("c", fake("third"), control=True))
    assert verdict_of(records, "c").control == DIFFERENT_AGAIN


def test_a_failed_control_says_nothing_and_changes_nothing():
    records = moment_with_control(page("c", [], control=True, outcome="timeout"))
    verdict = verdict_of(records, "c")
    assert (verdict.verdict, verdict.control) == (POISONED, NO_ANSWER)
    assert verdict_of(records, "a").control is None


# --- the figures on the page, and the command ------------------------------------------------


def test_the_poisoned_share_is_counted_on_resolved_pages_that_got_a_verdict():
    """The steps are a funnel: a page with an unresolved link already failed step 2 and is
    not counted again in step 3, although it is still compared (it is a witness for others)."""
    records = [
        page("a", REAL),
        page("b", REAL),
        page("c", fake("c")),
        page("a", REAL, task="task-2"),
        page("b", REAL, task="task-2", wrappers=1),
        page("c", fake("d"), task="task-2", wrappers=1),
        page("c", fake("e"), task="task-3"),  # alone: nothing to compare with
    ]
    verdicts = judge(records)
    assert verdicts[("run-1", "task-2", "c")].verdict == POISONED

    def figures(provider):
        return measure([r for r in records if r.provider == provider], verdicts)

    assert figures("a")["poisoned"][0] == 0.0 and figures("a")["poisoned"][3:] == [0, 2]
    assert figures("b")["poisoned"][3:] == [0, 1]  # its second page is unresolved
    c = figures("c")
    assert c["resolved"][3:] == [2, 3]
    assert c["poisoned"][3:] == [1, 1]  # task-1; task-2 is unresolved, task-3 is not judged
    assert c["unjudged"][3:] == [1, 2]
    assert c["usable"] is None  # still not measured: see summary.NOT_MEASURED_YET
    assert measure(records)["poisoned"] is None  # without verdicts there is no figure


def test_the_page_says_agreement_until_the_pages_have_a_history():
    today = [page("a", REAL), page("b", REAL), page("c", fake("c"))]
    stage = poison_stage(today, judge(today), DAY.date())
    assert (stage["stage"], stage["judged"], stage["judged_with_history"]) == ("agreement", 3, 0)

    with_history = [*history(providers=("a", "b", "c")), *today]
    stage = poison_stage(today, judge(with_history), DAY.date())
    assert (stage["stage"], stage["judged_with_history"]) == ("history", 3)


def test_the_poison_command_lists_the_pages_that_did_not_match(tmp_path, capsys):
    path = tmp_path / "raw" / "run-0000.jsonl.gz"
    path.parent.mkdir()
    with RecordWriter(path) as writer:
        for record in [
            page("a", REAL),
            page("b", REAL),
            page("c", fake("c")),
            page("c", REAL, control=True),
        ]:
            writer.write(record)
    assert cli.main(["poison", "--records", str(tmp_path / "raw"), "--sites"]) == 0
    out = capsys.readouterr().out
    assert out.count("POISONED  c  'personal loans online' US desktop p1") == 1
    # Three pages were measured; the control repeat is not a fourth.
    counted = [line.split() for line in out.splitlines() if line.startswith(("a ", "b ", "c "))]
    assert counted == [
        ["a", "1", "1", "0", "0", "0.0%"],
        ["b", "1", "1", "0", "0", "0.0%"],
        ["c", "1", "0", "1", "0", "100.0%"],
    ]
    assert "shared with: a* 0/10, b* 0/10" in out
    assert "repeat matched the others: a one-off" in out and "c0.net" in out
    assert cli.main(["poison", "--records", str(tmp_path / "raw"), "--provider", "a"]) == 0
    assert "POISONED" not in capsys.readouterr().out
