"""The planner: deterministic, and the arithmetic of pools adds up."""

from __future__ import annotations

import datetime as dt
import shutil
from collections import Counter

import pytest
import yaml
from conftest import DAY, ROOT

from serp_bench.config import ConfigError, load_config
from serp_bench.models import Device
from serp_bench.queries import (
    late_run,
    load_query_set,
    per_run_counts,
    plan,
    resolve_run,
    stored_runs,
)


@pytest.fixture
def config():
    return load_config(ROOT / "bench.yaml")


def custom(tmp_path, pools: dict, runs_per_day: int = 4):
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
    tasks = plan(config, ROOT, DAY, 1)
    expected = {
        name: per_run_counts(pool.per_day, config.schedule.runs_per_day)[1]
        for name, pool in config.pools.items()
    }
    assert Counter(t.pool for t in tasks) == expected
    assert len({t.task_id for t in tasks}) == len(tasks)
    assert all(t.headline == (t.pool != "page2") for t in tasks)
    assert {t.page for t in tasks if t.pool == "page2"} == {2}
    # Mobile searches are not measured: no pool of the shipped configuration asks for them.
    assert {t.device for t in tasks} == {Device.desktop}


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
    day = [task for run in runs for task in plan(config, ROOT, DAY, run)]
    assert Counter(t.pool for t in day) == {n: p.per_day for n, p in config.pools.items()}


def test_planning_is_deterministic(config):
    assert plan(config, ROOT, DAY, 2) == plan(config, ROOT, DAY, 2)


def test_the_plan_changes_with_the_day_the_run_and_the_seed(config):
    base = [t.query_id for t in plan(config, ROOT, DAY, 0)]
    assert base != [t.query_id for t in plan(config, ROOT, DAY + dt.timedelta(days=1), 0)]
    assert base != [t.query_id for t in plan(config, ROOT, DAY, 1)]
    assert base != [t.query_id for t in plan(config, ROOT, DAY, 0, seed=7)]


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

    day = [t for run in range(config.schedule.runs_per_day) for t in plan(config, ROOT, DAY, run)]
    assert {t.query_id for t in day if t.pool == "core"} == {q.id for q in core}
    tomorrow = [
        t
        for run in range(config.schedule.runs_per_day)
        for t in plan(config, ROOT, DAY + dt.timedelta(days=1), run)
    ]
    today_wide = {t.query_id for t in day if t.pool == "wide"}
    assert not today_wide & {t.query_id for t in tomorrow if t.pool == "wide"}


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


def test_task_ids_name_the_day_the_run_and_the_pool(config):
    task = plan(config, ROOT, DAY, 3)[0]
    assert task.task_id.startswith("20261112-r3-")


def test_resolve_run(config):
    now = dt.datetime(2026, 11, 12, 6, 30, tzinfo=dt.UTC)
    assert resolve_run(config, "2", now) == 2
    due = config.schedule.run_due_in_hour(now)
    assert resolve_run(config, "auto", now) == due
    quiet = dt.datetime(2026, 11, 12, (config.schedule.due_hour(DAY, 0) + 1) % 24, tzinfo=dt.UTC)
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
    now = dt.datetime(2026, 11, 12, 6, 30, tzinfo=dt.UTC)
    assert resolve_run(config, "next", now, day=DAY, stored=tmp_path) == 0
    _store(config, tmp_path, DAY, 0)
    _store(config, tmp_path, DAY, 2)
    assert stored_runs(config, DAY, tmp_path) == {0, 2}
    assert resolve_run(config, "next", now, day=DAY, stored=tmp_path) == 1
    for index in range(config.schedule.runs_per_day):
        _store(config, tmp_path, DAY, index)
    assert resolve_run(config, "next", now, day=DAY, stored=tmp_path) is None
    # Another day starts from its first run again.
    tomorrow = DAY + dt.timedelta(days=1)
    assert resolve_run(config, "next", now, day=tomorrow, stored=tmp_path) == 0


def test_the_schedule_does_a_run_however_late_the_job_starts(config, tmp_path):
    """GitHub starts a scheduled job tens of minutes late, often in the next clock hour.
    The run that fell due is still the one to do; asking for "the run due in this hour"
    would find nothing and lose it."""
    schedule = config.schedule
    due = schedule.due_at(DAY, 1)
    assert late_run(config, due - dt.timedelta(minutes=1), tmp_path) is None  # not due yet
    assert late_run(config, due, tmp_path) == (DAY, 1)
    assert late_run(config, due + dt.timedelta(minutes=40), tmp_path) == (DAY, 1)
    next_hour = due + dt.timedelta(minutes=55)
    assert next_hour.hour != due.hour and late_run(config, next_hour, tmp_path) == (DAY, 1)
    # A job that failed is tried again by the next hourly start, as long as the run is owed.
    assert late_run(config, due + schedule.grace, tmp_path) == (DAY, 1)
    # GitHub also drops hourly starts: more than three hours can pass between two of them.
    assert late_run(config, due + dt.timedelta(hours=3, minutes=30), tmp_path) == (DAY, 1)
    # Later than that the run is let go: it must not land on the following one.
    assert late_run(config, due + schedule.grace + dt.timedelta(minutes=1), tmp_path) is None
    assert schedule.grace < dt.timedelta(hours=schedule.step_hours)
    # Once it is stored it is not owed any more, however early in its window.
    _store(config, tmp_path, DAY, 1)
    assert late_run(config, due + dt.timedelta(minutes=40), tmp_path) is None


def test_the_last_run_of_a_day_can_be_done_after_midnight(config, tmp_path):
    """The run keeps its own day: its file and its queries are yesterday's."""
    last = config.schedule.runs_per_day - 1
    day = next(  # a day whose last run falls due less than the grace before midnight
        d
        for d in (DAY + dt.timedelta(days=n) for n in range(30))
        if (config.schedule.due_at(d, last) + config.schedule.grace).date() > d
    )
    after_midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 5), tzinfo=dt.UTC)
    assert late_run(config, after_midnight, tmp_path) == (day, last)
    _store(config, tmp_path, day, last)
    assert late_run(config, after_midnight, tmp_path) is None


def test_auto_does_not_repeat_a_run_that_is_stored(config, tmp_path):
    """The schedule must not do again the run someone already did by hand."""
    now = dt.datetime(2026, 11, 12, config.schedule.due_hour(DAY, 1), 30, tzinfo=dt.UTC)
    assert resolve_run(config, "auto", now, day=DAY, stored=tmp_path) == 1
    _store(config, tmp_path, DAY, 1)
    assert resolve_run(config, "auto", now, day=DAY, stored=tmp_path) is None
    assert resolve_run(config, "1", now, day=DAY, stored=tmp_path) == 1  # a number is obeyed


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
