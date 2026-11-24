"""The planner: deterministic, and the arithmetic of pools adds up."""

from __future__ import annotations

import datetime as dt
import itertools
import shutil
from collections import Counter

import pytest
import yaml
from conftest import BENCH, DAY, NEW_DAY, ROOT

from serp_bench.config import ConfigError, load_config
from serp_bench.models import Device
from serp_bench.queries import (
    REPEAT_POOL,
    late_run,
    load_query_set,
    per_run_counts,
    plan,
    provider_order,
    resolve_run,
    stored_runs,
)


@pytest.fixture
def config():
    """The shipped configuration: in force from NEW_DAY."""
    return load_config(BENCH)


@pytest.fixture
def before(config):
    """The configuration before it: in force on DAY."""
    return config.on(DAY)


def custom(tmp_path, pools: dict, runs_per_day: int = 4, **more: object):
    shutil.copytree(ROOT / "queries", tmp_path / "queries", dirs_exist_ok=True)
    # A list of exactly twenty, for the tests whose arithmetic depends on the size.
    twenty = [
        {"id": f"q-{i:02d}", "query": f"query {i}", "country": "US", "locale": "en"}
        for i in range(1, 21)
    ]
    (tmp_path / "queries" / "twenty.yaml").write_text(
        yaml.safe_dump({"version": 1, "queries": twenty}), encoding="utf-8"
    )
    data = {
        "methodology_version": "test",
        "providers": ["mock"],
        "schedule": {"runs_per_day": runs_per_day},
        "pools": pools,
        **more,
    }
    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


@pytest.mark.parametrize(
    ("per_day", "runs", "expected"),
    [(160, 4, [40, 40, 40, 40]), (10, 4, [3, 3, 2, 2]), (2, 4, [1, 1, 0, 0]), (7, 1, [7])],
)
def test_per_run_counts(per_day, runs, expected):
    assert per_run_counts(per_day, runs) == expected
    assert sum(expected) == per_day


def test_a_run_has_each_pools_share(config):
    tasks = plan(config, ROOT, NEW_DAY, 1)
    runs = config.schedule.runs_per_day
    assert Counter(t.pool for t in tasks) == {
        "core": per_run_counts(40, runs)[1],
        "wide": per_run_counts(40, runs)[1],
        "city": 1,
        "deep": 4,  # pages 2 to 5 of one search
        "week": 1,
        REPEAT_POOL: 5,  # three first pages and two later pages, asked again
    }
    assert len({t.task_id for t in tasks}) == len(tasks)
    assert all(t.headline == (t.pool in ("core", "wide")) for t in tasks)
    assert sorted(t.page for t in tasks if t.pool == "deep") == [2, 3, 4, 5]
    # Mobile searches are not measured: no pool of the shipped configuration asks for them.
    assert {t.device for t in tasks} == {Device.desktop}


def test_a_run_of_the_configuration_before_has_each_pools_share(before):
    tasks = plan(before, ROOT, DAY, 1)
    expected = {
        name: per_run_counts(pool.per_day, before.schedule.runs_per_day)[1]
        for name, pool in before.pools.items()
    }
    assert Counter(t.pool for t in tasks) == expected == {"core": 10, "wide": 10, "page2": 4}
    assert all(t.headline == (t.pool != "page2") for t in tasks)
    assert {t.page for t in tasks if t.pool == "page2"} == {2}


def test_the_plan_of_the_configuration_before_is_what_it_always_was(config):
    """The published records of methodology 0.1 can still be recomputed: the same tasks in
    the same order, and no timetable (a task's place in the list is its place in time)."""
    day = dt.date(2026, 11, 23)
    tasks = plan(config.on(day), ROOT, day, 2)
    assert Counter(t.pool for t in tasks) == {"core": 10, "wide": 10, "page2": 4}
    assert [(t.task_id, t.query_id) for t in tasks[:3]] == [
        ("20261123-r2-core-003", "br-004"),
        ("20261123-r2-core-006", "fr-001"),
        ("20261123-r2-wide-004", "us-043"),
    ]
    assert all(t.tick is None and t.repeat_of is None for t in tasks)
    assert all(t.feature is None and t.time_range is None and t.location is None for t in tasks)


def test_a_pool_can_ask_for_mobile_searches(tmp_path):
    """The planner still knows how: a provider's capability is checked in conformance."""
    pools = {
        "core": {"source": "queries/core.yaml", "per_day": 8},
        "phone": {"from": "core", "per_day": 4, "device": "mobile"},
    }
    tasks = plan(custom(tmp_path, pools), tmp_path, DAY, 0)
    assert {t.device for t in tasks if t.pool == "phone"} == {Device.mobile}


def test_a_day_adds_up_to_per_day(config):
    runs = range(config.schedule.runs_per_day)
    day = [task for run in runs for task in plan(config, ROOT, NEW_DAY, run)]
    expected = {name: config.tasks_per_day(name) for name in config.pools}
    assert Counter(t.pool for t in day) == expected | {REPEAT_POOL: config.repeats_per_day}
    assert len(day) == 40 + 40 + 12 + 3 + 3 + 15


def test_a_day_of_the_configuration_before_adds_up_to_per_day(before):
    runs = range(before.schedule.runs_per_day)
    day = [task for run in runs for task in plan(before, ROOT, DAY, run)]
    assert Counter(t.pool for t in day) == {n: p.per_day for n, p in before.pools.items()}


def test_planning_is_deterministic(config, before):
    assert plan(config, ROOT, NEW_DAY, 2) == plan(config, ROOT, NEW_DAY, 2)
    assert plan(before, ROOT, DAY, 2) == plan(before, ROOT, DAY, 2)


def test_the_plan_changes_with_the_day_the_run_and_the_seed(config):
    base = [t.query_id for t in plan(config, ROOT, NEW_DAY, 0)]
    assert base != [t.query_id for t in plan(config, ROOT, NEW_DAY + dt.timedelta(days=1), 0)]
    assert base != [t.query_id for t in plan(config, ROOT, NEW_DAY, 1)]
    assert base != [t.query_id for t in plan(config, ROOT, NEW_DAY, 0, seed=7)]


def test_each_query_once_a_day_when_per_day_equals_the_set(tmp_path):
    queries = load_query_set(ROOT / "queries" / "core.yaml")
    config = custom(tmp_path, {"core": {"source": "queries/core.yaml", "per_day": len(queries)}})
    day = [t.query_id for run in range(4) for t in plan(config, tmp_path, DAY, run)]
    assert sorted(day) == sorted(q.id for q in queries)


def test_a_query_moves_between_runs_from_day_to_day(tmp_path):
    queries = load_query_set(ROOT / "queries" / "core.yaml")
    config = custom(tmp_path, {"core": {"source": "queries/core.yaml", "per_day": len(queries)}})
    seen = set()
    for offset in range(10):
        for run in range(4):
            tasks = plan(config, tmp_path, DAY + dt.timedelta(days=offset), run)
            if queries[0].id in {t.query_id for t in tasks}:
                seen.add(run)
    assert len(seen) > 1


def test_a_rotating_list_brings_every_query_back_after_exactly_one_cycle(tmp_path):
    """Twenty queries, five a day: each one every fourth day, never sooner and never later.
    A per-query history depends on that; a list reshuffled whole on every pass would bring
    a query back after one day or after seven."""
    config = custom(tmp_path, {"wide": {"source": "queries/twenty.yaml", "per_day": 5}})
    sent: dict[str, list[int]] = {}
    slots: dict[str, set[int]] = {}
    for offset in range(24):
        for run in range(4):
            for task in plan(config, tmp_path, DAY + dt.timedelta(days=offset), run):
                sent.setdefault(task.query_id, []).append(offset)
                slots.setdefault(task.query_id, set()).add(run)
    assert len(sent) == 20
    gaps = {days[i + 1] - days[i] for days in sent.values() for i in range(len(days) - 1)}
    assert gaps == {4}
    # Its day of the cycle is fixed; its run within that day is not.
    assert any(len(runs) > 1 for runs in slots.values())


def test_the_shipped_lists_are_a_core_sent_daily_and_a_wide_list_that_rotates(config):
    """What the methodology says about the searches, checked against the files."""
    core = load_query_set(ROOT / config.source_of("core"))
    wide = load_query_set(ROOT / config.source_of("wide"))
    assert len(core) == config.pools["core"].per_day  # every core search every day
    cycle, rest = divmod(len(wide), config.pools["wide"].per_day)
    assert rest == 0 and cycle == 4  # every wide search every four days, exactly
    both = [*core, *wide]
    assert len({q.id for q in both}) == len(both)
    assert len({(q.query.casefold(), q.country) for q in both}) == len(both)  # no search twice
    commercial = sum(1 for q in both if q.category == "commercial")
    assert commercial * 2 >= len(both)  # "about half are commercial" is at least half

    runs = range(config.schedule.runs_per_day)
    day = [t for run in runs for t in plan(config, ROOT, NEW_DAY, run)]
    assert {t.query_id for t in day if t.pool == "core"} == {q.id for q in core}
    tomorrow = [t for run in runs for t in plan(config, ROOT, NEW_DAY + dt.timedelta(days=1), run)]
    today_wide = {t.query_id for t in day if t.pool == "wide"}
    assert not today_wide & {t.query_id for t in tomorrow if t.pool == "wide"}


def test_the_core_list_keeps_most_of_the_list_before_it_and_adds_what_real_use_has(config, before):
    """Thirty-one searches go on under their ids, so their records go on without a break.
    Nine are new: three inside one site, three for an exact phrase, three longer ones."""
    core = {q.id: q for q in load_query_set(ROOT / config.source_of("core"))}
    was = {q.id: q for q in load_query_set(ROOT / before.source_of("core"))}
    kept = set(core) & set(was)
    assert len(kept) == 31 and all(core[i] == was[i] for i in kept)
    new = [core[i].query for i in sorted(set(core) - set(was))]
    assert len(new) == 9
    assert sum(q.startswith("site:") for q in new) == 3
    assert sum(q.startswith('"') and q.endswith('"') for q in new) == 3
    assert sum(len(q.split()) >= 7 for q in new) == 3


def test_the_city_list_is_ten_searches_placed_in_each_of_five_cities(config):
    city = load_query_set(ROOT / config.source_of("city"))
    places = Counter(q.location for q in city)
    assert len(places) == 5 and set(places.values()) == {10}
    assert len({q.query for q in city}) == 10
    # Each place is given both ways: by Google's name for it and as a point on the map.
    assert len({(q.location, q.coordinates) for q in city}) == 5
    assert all(q.location.count(",") == 2 and q.coordinates is not None for q in city)
    # No search of it names a place in its words: the place is in how it is asked.
    assert not any(q.location.split(",")[0].casefold() in q.query.casefold() for q in city)
    # Neither the core nor the wide list places a search anywhere below its country.
    plain = [*load_query_set(ROOT / config.source_of("core"))]
    plain += load_query_set(ROOT / config.source_of("wide"))
    assert not any(q.location or q.coordinates for q in plain)


def test_a_small_set_cycles(tmp_path):
    config = custom(tmp_path, {"core": {"source": "queries/twenty.yaml", "per_day": 100}}, 1)
    counts = Counter(t.query_id for t in plan(config, tmp_path, DAY, 0))
    assert set(counts.values()) == {5}  # 20 queries, 100 tasks


def test_a_pool_only_takes_part_in_its_runs(tmp_path):
    config = custom(
        tmp_path,
        {
            "core": {"source": "queries/core.yaml", "per_day": 8},
            "nightly": {"from": "core", "per_day": 5, "runs": [0], "page": 2},
        },
    )
    assert Counter(t.pool for t in plan(config, tmp_path, DAY, 0)) == {"core": 2, "nightly": 5}
    assert Counter(t.pool for t in plan(config, tmp_path, DAY, 3)) == {"core": 2}


def test_task_ids_name_the_day_the_run_and_the_pool(config, before):
    tasks = [t for t in plan(config, ROOT, NEW_DAY, 2) if t.pool != REPEAT_POOL]
    assert all(t.task_id.startswith(f"20261126-r2-{t.pool}-") for t in tasks)
    assert plan(before, ROOT, DAY, 3)[0].task_id.startswith("20261112-r3-")


def test_a_run_the_schedule_does_not_have_cannot_be_planned(config, before):
    assert len(plan(before, ROOT, DAY, 3)) == 24  # four runs a day before
    with pytest.raises(ConfigError, match=r"run index 3 is not in 0\.\.2"):
        plan(config, ROOT, NEW_DAY, 3)  # three now


def test_resolve_run(config):
    now = dt.datetime.combine(NEW_DAY, dt.time(6, 30), tzinfo=dt.UTC)
    assert resolve_run(config, "2", now) == 2
    due = config.schedule.run_due_in_hour(now)
    assert resolve_run(config, "auto", now) == due
    hour = (config.schedule.due_hour(NEW_DAY, 0) + 1) % 24
    quiet = dt.datetime.combine(NEW_DAY, dt.time(hour), tzinfo=dt.UTC)
    assert resolve_run(config, "auto", quiet) is None
    for bad in (str(config.schedule.runs_per_day), "-1", "noon"):
        with pytest.raises(ConfigError):
            resolve_run(config, bad, now)


def _store(config, folder, day, index):
    """A record file for one run, laid out as on the data branch."""
    due = config.schedule.due_time(day, index).replace(":", "")
    path = folder / f"{day:%Y/%m/%d}" / f"run-{due}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_next_is_the_first_run_of_the_day_that_is_not_stored(config, tmp_path):
    """A run started by hand needs no index: it does what is left of the day, in order."""
    now = dt.datetime.combine(NEW_DAY, dt.time(6, 30), tzinfo=dt.UTC)
    assert resolve_run(config, "next", now, day=NEW_DAY, stored=tmp_path) == 0
    _store(config, tmp_path, NEW_DAY, 0)
    _store(config, tmp_path, NEW_DAY, 2)
    assert stored_runs(config, NEW_DAY, tmp_path) == {0, 2}
    assert resolve_run(config, "next", now, day=NEW_DAY, stored=tmp_path) == 1
    for index in range(config.schedule.runs_per_day):
        _store(config, tmp_path, NEW_DAY, index)
    assert resolve_run(config, "next", now, day=NEW_DAY, stored=tmp_path) is None
    # Another day starts from its first run again.
    tomorrow = NEW_DAY + dt.timedelta(days=1)
    assert resolve_run(config, "next", now, day=tomorrow, stored=tmp_path) == 0


def test_the_schedule_does_a_run_however_late_the_job_starts(config, tmp_path):
    """GitHub starts a scheduled job tens of minutes late, often in the next clock hour.
    The run that fell due is still the one to do; asking for "the run due in this hour"
    would find nothing and lose it."""
    schedule = config.schedule
    due = schedule.due_at(NEW_DAY, 1)
    assert late_run(config, due - dt.timedelta(minutes=1), tmp_path) is None  # not due yet
    assert late_run(config, due, tmp_path) == (NEW_DAY, 1)
    assert late_run(config, due + dt.timedelta(minutes=40), tmp_path) == (NEW_DAY, 1)
    next_hour = due + dt.timedelta(minutes=55)
    assert next_hour.hour != due.hour and late_run(config, next_hour, tmp_path) == (NEW_DAY, 1)
    # A job that failed is tried again by the next hourly start, as long as the run is owed.
    assert late_run(config, due + schedule.grace, tmp_path) == (NEW_DAY, 1)
    # GitHub also drops hourly starts: more than three hours can pass between two of them.
    assert late_run(config, due + dt.timedelta(hours=3, minutes=30), tmp_path) == (NEW_DAY, 1)
    assert schedule.grace == dt.timedelta(hours=7)  # three runs a day: eight hours less one
    # Later than that the run is let go: it must not land on the following one.
    assert late_run(config, due + schedule.grace + dt.timedelta(minutes=1), tmp_path) is None
    assert schedule.grace < dt.timedelta(hours=schedule.step_hours)
    # Once it is stored it is not owed any more, however early in its window.
    _store(config, tmp_path, NEW_DAY, 1)
    assert late_run(config, due + dt.timedelta(minutes=40), tmp_path) is None


def test_the_last_run_of_a_day_can_be_done_after_midnight(config, tmp_path):
    """The run keeps its own day: its file and its queries are yesterday's."""
    last = config.schedule.runs_per_day - 1
    day = next(  # a day whose last run falls due less than the grace before midnight
        d
        for d in (NEW_DAY + dt.timedelta(days=n) for n in range(30))
        if (config.schedule.due_at(d, last) + config.schedule.grace).date() > d
    )
    after_midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 5), tzinfo=dt.UTC)
    assert late_run(config, after_midnight, tmp_path) == (day, last)
    _store(config, tmp_path, day, last)
    assert late_run(config, after_midnight, tmp_path) is None


def test_auto_does_not_repeat_a_run_that_is_stored(config, tmp_path):
    """The schedule must not do again the run someone already did by hand."""
    now = dt.datetime.combine(
        NEW_DAY, dt.time(config.schedule.due_hour(NEW_DAY, 1), 30), tzinfo=dt.UTC
    )
    assert resolve_run(config, "auto", now, day=NEW_DAY, stored=tmp_path) == 1
    _store(config, tmp_path, NEW_DAY, 1)
    assert resolve_run(config, "auto", now, day=NEW_DAY, stored=tmp_path) is None
    assert resolve_run(config, "1", now, day=NEW_DAY, stored=tmp_path) == 1  # a number is obeyed


def test_a_day_is_owed_the_runs_of_its_own_configuration(config, before, tmp_path):
    """Around the day the shipped file starts. The day before has four runs and the last of
    them is still owed after midnight; the first day has three, eight hours apart."""
    eve = NEW_DAY - dt.timedelta(days=1)
    last_before = before.schedule.due_at(eve, 3)
    midnight = dt.datetime.combine(NEW_DAY, dt.time(0, 0), tzinfo=dt.UTC)
    assert last_before + before.schedule.grace > midnight
    assert late_run(config, midnight + dt.timedelta(minutes=5), tmp_path) == (eve, 3)
    assert late_run(config, last_before - dt.timedelta(hours=1), tmp_path) == (eve, 2)
    # Stored under the times of its own day's schedule, it is not owed any more.
    _store(before, tmp_path, eve, 3)
    assert late_run(config, midnight + dt.timedelta(minutes=5), tmp_path) is None

    first = config.schedule.due_at(NEW_DAY, 0)
    assert late_run(config, first, tmp_path) == (NEW_DAY, 0)
    # Six hours on, the schedule before would have a second run due. This day has none yet.
    assert before.schedule.due_at(NEW_DAY, 1) == first + dt.timedelta(hours=6)
    assert late_run(config, first + dt.timedelta(hours=6, minutes=30), tmp_path) == (NEW_DAY, 0)
    _store(config, tmp_path, NEW_DAY, 0)
    assert late_run(config, first + dt.timedelta(hours=6, minutes=30), tmp_path) is None
    assert late_run(config, first + dt.timedelta(hours=8), tmp_path) == (NEW_DAY, 1)


def test_query_files_are_validated(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "queries": [
                    {"id": "a", "query": "x", "country": "US", "locale": "en"},
                    {"id": "a", "query": "y", "country": "US", "locale": "en"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate query ids"):
        load_query_set(path)
    path.write_text(
        yaml.safe_dump(
            {"version": 1, "queries": [{"id": "a", "query": "x", "country": "usa", "locale": "en"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="queries.0.country"):
        load_query_set(path)


def test_a_query_can_be_placed_by_name_and_by_coordinates(tmp_path):
    path = tmp_path / "q.yaml"
    placed = {"id": "a", "query": "x", "country": "US", "locale": "en"}
    placed |= {"location": "Austin,Texas,United States", "coordinates": [30.2672, -97.7431]}
    plain = {"id": "b", "query": "y", "country": "US", "locale": "en"}
    path.write_text(yaml.safe_dump({"version": 1, "queries": [placed, plain]}), encoding="utf-8")
    first, second = load_query_set(path)
    assert first.location == "Austin,Texas,United States"
    assert first.coordinates == (30.2672, -97.7431)
    assert second.location is None and second.coordinates is None
    placed["coordinates"] = [30.2672]  # a point is a latitude and a longitude
    path.write_text(yaml.safe_dump({"version": 1, "queries": [placed]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="queries.0.coordinates"):
        load_query_set(path)


# --- a list larger than a day's share: every query equally often, none skipped -------------


def day_ids(config, root, day, pool="core"):
    return [
        t.query_id
        for run in range(config.schedule.runs_per_day)
        for t in plan(config, root, day, run)
        if t.pool == pool
    ]


def test_half_the_list_a_day_means_each_query_exactly_every_second_day(tmp_path):
    """20 queries, 10 a day: any two consecutive days that start a pass cover the whole
    list once, and over 14 days every query is sent exactly 7 times."""
    config = custom(tmp_path, {"core": {"source": "queries/twenty.yaml", "per_day": 10}})
    everything = {q.id for q in load_query_set(tmp_path / "queries/twenty.yaml")}
    assert len(everything) == 20

    counts: Counter[str] = Counter()
    last_seen: dict[str, int] = {}
    longest_gap = 0
    for offset in range(14):
        for query_id in day_ids(config, tmp_path, dt.date(2026, 1, 1) + dt.timedelta(days=offset)):
            if query_id in last_seen:
                longest_gap = max(longest_gap, offset - last_seen[query_id])
            last_seen[query_id] = offset
            counts[query_id] += 1
    assert set(counts) == everything
    assert set(counts.values()) == {7}, "every query is sent equally often"
    assert longest_gap <= 3, "a query is never left out for long (passes are reshuffled)"


def test_a_day_never_repeats_a_query_while_the_list_is_larger_than_the_day(tmp_path):
    config = custom(tmp_path, {"core": {"source": "queries/core.yaml", "per_day": 10}})
    for offset in range(6):
        ids = day_ids(config, tmp_path, dt.date(2026, 3, 1) + dt.timedelta(days=offset))
        assert len(ids) == 10
        # a day may straddle two passes only when per_day does not divide the list; here it does
        assert len(set(ids)) == 10


def test_an_uneven_share_still_sends_every_query_equally_often(tmp_path):
    """20 queries, 7 a day: passes straddle days, yet after 20 days each was sent 7 times."""
    config = custom(tmp_path, {"core": {"source": "queries/twenty.yaml", "per_day": 7}})
    counts: Counter[str] = Counter()
    for offset in range(20):
        counts.update(day_ids(config, tmp_path, dt.date(2026, 1, 1) + dt.timedelta(days=offset)))
    assert len(counts) == 20 and set(counts.values()) == {7}


# --- a timetable: later pages in order, and searches asked again ---------------------------


def runs_of(config, days: int = 4):
    """The runs of the first days of the shipped configuration, in order."""
    return [
        (NEW_DAY + dt.timedelta(days=offset), run)
        for offset in range(days)
        for run in range(config.schedule.runs_per_day)
    ]


def paged_search(tasks):
    """The search of a run that is asked for page after page: its first page, then the rest."""
    later = sorted((t for t in tasks if t.pool == "deep"), key=lambda t: t.page)
    (first,) = [t for t in tasks if t.pool == "core" and t.query_id == later[0].query_id]
    return [first, *later]


def test_every_task_of_a_timetable_has_a_tick_of_its_own(config):
    for day, run in runs_of(config):
        ticks = [t.tick for t in plan(config, ROOT, day, run)]
        assert None not in ticks and len(set(ticks)) == len(ticks)
        assert ticks == sorted(ticks) and ticks[0] == 0  # the list is in the order of sending


def test_a_plan_without_later_pages_or_repeats_has_no_timetable(tmp_path):
    pools = {
        "core": {"source": "queries/core.yaml", "per_day": 8},
        "second": {"follows": "core", "per_run": 1, "page": 2},
    }
    tasks = plan(custom(tmp_path, pools), tmp_path, DAY, 0)
    assert len(tasks) == 3 and all(t.tick is None for t in tasks)


def test_the_pages_of_a_search_follow_its_first_page_in_order(config):
    """One after another and never two at a time, the way a person turns pages: each is
    asked for only after the time limit of the page before it has passed."""
    assert config.page_gap_ticks == 5
    for day, run in runs_of(config):
        chain = paged_search(plan(config, ROOT, day, run))
        assert [t.page for t in chain] == [1, 2, 3, 4, 5]
        assert {b.tick - a.tick for a, b in itertools.pairwise(chain)} == {5}
        assert len({(t.query_id, t.query, t.country, t.locale, t.device) for t in chain}) == 1
        assert [t.task_id for t in chain[1:]] == [
            f"{day:%Y%m%d}-r{run}-deep-001-p{page}" for page in (2, 3, 4, 5)
        ]


def test_a_repeat_is_sent_a_fixed_time_after_the_search_it_repeats(config):
    assert config.repeat_ticks == 20  # five minutes
    for day, run in runs_of(config):
        tasks = plan(config, ROOT, day, run)
        by_id = {t.task_id: t for t in tasks}
        repeats = [t for t in tasks if t.repeat_of is not None]
        assert len(repeats) == 5
        assert all(t.tick - by_id[t.repeat_of].tick == 20 for t in repeats)


def test_a_repeat_is_the_same_search_under_a_name_of_its_own(config):
    tasks = plan(config, ROOT, NEW_DAY, 0)
    by_id = {t.task_id: t for t in tasks}
    repeats = [t for t in tasks if t.repeat_of is not None]
    own = {"task_id", "pool", "headline", "feature", "repeat_of", "tick"}
    for again in repeats:
        origin = by_id[again.repeat_of]
        assert again.task_id == f"{origin.task_id}-again"
        assert (again.pool, again.headline, again.feature) == (REPEAT_POOL, False, None)
        assert again.model_dump(exclude=own) == origin.model_dump(exclude=own)
    # Only a repeat is in the pool of repeats, and nothing is asked a third time.
    assert [t for t in tasks if t.pool == REPEAT_POOL] == repeats
    assert all(by_id[t.repeat_of].repeat_of is None for t in repeats)
    assert len({t.repeat_of for t in repeats}) == len(repeats)


def test_a_run_asks_again_for_three_first_pages_and_two_later_pages(config):
    for day, run in runs_of(config):
        tasks = plan(config, ROOT, day, run)
        by_id = {t.task_id: t for t in tasks}
        origins = [by_id[t.repeat_of] for t in tasks if t.repeat_of is not None]
        first = [t for t in origins if t.page == 1]
        assert len(first) == 3 and all(t.headline for t in first)
        assert [t.pool for t in origins if t.page > 1] == ["deep", "deep"]


def test_the_later_pages_asked_again_alternate_from_run_to_run(config):
    """Pages 2 and 4 in one run, 3 and 5 in the next, also across midnight: over any two
    runs in a row every later page has had its repeat."""
    asked = []
    for day, run in runs_of(config):
        tasks = plan(config, ROOT, day, run)
        asked.append(sorted(t.page for t in tasks if t.repeat_of is not None and t.page > 1))
    assert set(map(tuple, asked)) == {(2, 4), (3, 5)}
    assert all(a != b for a, b in itertools.pairwise(asked))


def test_the_past_week_search_is_a_core_search_of_the_run_and_not_the_paged_one(config):
    """It is read against the same search without the filter, sent in the same run."""
    for day, run in runs_of(config):
        tasks = plan(config, ROOT, day, run)
        (week,) = [t for t in tasks if t.pool == "week"]
        (plain,) = [t for t in tasks if t.pool == "core" and t.query_id == week.query_id]
        assert (week.time_range, week.feature, week.headline, week.page) == (
            "week",
            "week",
            False,
            1,
        )
        assert (plain.time_range, plain.feature, plain.headline) == (None, None, True)
        assert (week.query, week.country, week.locale) == (plain.query, plain.country, plain.locale)
        assert week.query_id != paged_search(tasks)[0].query_id


def test_only_the_past_week_search_asks_for_a_time_range(config):
    tasks = plan(config, ROOT, NEW_DAY, 0)
    assert [t.pool for t in tasks if t.time_range is not None] == ["week"]


def test_the_city_search_carries_its_place_by_name_and_as_coordinates(config):
    places = {q.id: q for q in load_query_set(ROOT / config.source_of("city"))}
    for day, run in runs_of(config):
        tasks = plan(config, ROOT, day, run)
        (city,) = [t for t in tasks if t.pool == "city"]
        query = places[city.query_id]
        assert (city.location, city.coordinates) == (query.location, query.coordinates)
        assert (city.feature, city.headline, city.page, city.time_range) == ("city", False, 1, None)
        assert all(t.location is None and t.coordinates is None for t in tasks if t is not city)


def test_two_pools_that_follow_one_pool_take_different_searches_of_the_run(tmp_path):
    pools = {
        "core": {"source": "queries/twenty.yaml", "per_day": 8},
        "second": {"follows": "core", "per_run": 1, "page": 2},
        "recent": {"follows": "core", "per_run": 1, "time_range": "week"},
    }
    config = custom(tmp_path, pools)
    for run in range(4):
        tasks = plan(config, tmp_path, DAY, run)
        sent = {t.query_id for t in tasks if t.pool == "core"}
        (second,) = [t for t in tasks if t.pool == "second"]
        (recent,) = [t for t in tasks if t.pool == "recent"]
        assert len(sent) == 2 and {second.query_id, recent.query_id} == sent
        assert (second.page, second.time_range) == (2, None)
        assert (recent.page, recent.time_range) == (1, "week")


def test_a_pool_cannot_follow_more_searches_than_the_run_has(tmp_path):
    pools = {
        "core": {"source": "queries/twenty.yaml", "per_day": 8},
        "second": {"follows": "core", "per_run": 5, "page": 2},
    }
    tasks = plan(custom(tmp_path, pools), tmp_path, DAY, 0)
    assert Counter(t.pool for t in tasks) == {"core": 2, "second": 2}


def test_first_pages_can_be_asked_again_without_any_later_pages(tmp_path):
    """Sixty seconds at one task every fifteen: the repeat is four places after its search."""
    pools = {"core": {"source": "queries/twenty.yaml", "per_day": 20, "headline": True}}
    repeats = {"after_seconds": 60, "first_pages": 2}
    config = custom(tmp_path, pools, runs_per_day=1, repeats=repeats)
    tasks = plan(config, tmp_path, DAY, 0)
    by_id = {t.task_id: t for t in tasks}
    again = [t for t in tasks if t.repeat_of is not None]
    assert len(tasks) == 22 and len(again) == 2
    assert [t.tick for t in tasks] == list(range(22))  # no place of the timetable is left empty
    assert all(t.tick - by_id[t.repeat_of].tick == 4 for t in again)


def test_a_search_outside_the_main_table_is_not_one_of_the_first_pages_asked_again(tmp_path):
    pools = {
        "core": {"source": "queries/twenty.yaml", "per_day": 2, "headline": True},
        "side": {"from": "core", "per_day": 10},
    }
    repeats = {"after_seconds": 60, "first_pages": 5}
    config = custom(tmp_path, pools, runs_per_day=1, repeats=repeats)
    tasks = plan(config, tmp_path, DAY, 0)
    by_id = {t.task_id: t for t in tasks}
    again = [t for t in tasks if t.repeat_of is not None]
    assert sorted(by_id[t.repeat_of].pool for t in again) == ["core", "core"]


# --- the order of the providers --------------------------------------------------------------

NAMES = ["delta", "alpha", "echo", "charlie", "bravo", "foxtrot"]


def test_the_order_of_the_providers_can_be_recomputed(config):
    order = provider_order(config, NEW_DAY, 1, NAMES)
    assert order == provider_order(config, NEW_DAY, 1, NAMES)
    assert sorted(order) == sorted(NAMES)  # every provider once
    # It follows from the date and the run alone, not from how the providers were listed.
    assert order == provider_order(config, NEW_DAY, 1, NAMES[::-1])


def test_the_order_is_drawn_anew_for_every_run(config):
    orders = [provider_order(config, day, run, NAMES) for day, run in runs_of(config)]
    assert all(a != b for a, b in itertools.pairwise(orders))
    assert provider_order(config, NEW_DAY, 0, NAMES) != provider_order(
        config, NEW_DAY, 0, NAMES, seed=7
    )


def test_no_provider_is_always_first(config):
    """Over a month of the shipped schedule every provider has been first and has been last."""
    orders = [
        provider_order(config, day, run, config.providers) for day, run in runs_of(config, 30)
    ]
    assert {order[0] for order in orders} == set(config.providers)
    assert {order[-1] for order in orders} == set(config.providers)


def test_without_staggering_the_order_is_the_one_given(before):
    """Every provider receives a task at the same instant, so the order means nothing."""
    assert not before.limits.stagger_providers
    for run in range(4):
        assert provider_order(before, DAY, run, NAMES) == NAMES
