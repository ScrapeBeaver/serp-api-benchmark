"""bench.yaml: the schedule, the pools and readable errors."""

from __future__ import annotations

import datetime as dt
import shutil

import pytest
import yaml
from conftest import BENCH, DAY, NEW_DAY, ROOT

from serp_bench.config import ConfigError, Limits, Schedule, config_hash, load_config


def write(tmp_path, data: dict, name: str = "bench.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def base() -> dict:
    """The shipped configuration, with a mobile pool added: the tests below need a second
    pool that borrows its queries, whatever pools the shipped file has today. It stands
    alone: the file before it is left out, and has tests of its own."""
    data = yaml.safe_load(BENCH.read_text(encoding="utf-8"))
    del data["starts"], data["previous"]
    data["pools"]["mobile"] = {"from": "core", "per_day": 8, "device": "mobile", "headline": False}
    return data


def with_pool(**pool: object) -> dict:
    """The base configuration with one more pool, `extra`."""
    data = base()
    data["pools"]["extra"] = pool
    return data


def error(tmp_path, data: dict) -> str:
    with pytest.raises(ConfigError) as caught:
        load_config(write(tmp_path, data))
    return str(caught.value)


def test_the_shipped_configuration_loads():
    config = load_config(BENCH)
    assert config.methodology_version == "0.2" and config.schedule.runs_per_day == 3
    assert set(config.pools) == {"core", "wide", "deep", "week", "city"}
    assert [name for name, pool in config.pools.items() if pool.headline] == ["core", "wide"]
    assert config.source_of("core") == "queries/core-0.2.yaml"
    assert config.source_of("wide") == "queries/wide-0.2.yaml"
    assert config.source_of("city") == "queries/city.yaml"
    assert config.pool_runs("core") == list(range(config.schedule.runs_per_day))  # every run


def test_the_shipped_pools_around_the_main_table():
    """Later pages and a past-week search of a core search of the same run, and a search
    placed in a city: none of them in the main table."""
    pools = load_config(BENCH).pools
    deep, week, city = pools["deep"], pools["week"], pools["city"]
    assert (deep.follows, deep.per_run, deep.pages) == ("core", 1, [2, 3, 4, 5])
    assert (week.follows, week.per_run, week.time_range, week.feature) == (
        "core",
        1,
        "week",
        "week",
    )
    assert (city.per_day, city.feature, city.time_range) == (3, "city", None)


def test_the_shipped_volume_of_a_day():
    config = load_config(BENCH)
    assert {name: config.tasks_per_day(name) for name in config.pools} == {
        "core": 40,
        "wide": 40,
        "deep": 12,  # four pages of one search, in each of three runs
        "week": 3,
        "city": 3,
    }
    assert config.repeats is not None
    assert (config.repeats.first_pages, config.repeats.later_pages) == (3, 2)
    assert config.repeats_per_day == 15  # five a run


def test_the_shipped_timetable_settings():
    config = load_config(BENCH)
    assert config.limits.stagger_providers is True
    assert config.repeats is not None and config.repeats.after_seconds == 300
    assert config.repeat_ticks == 20  # 300 s at one task every 15 s
    assert config.page_gap_ticks == 5  # 60 s of time limit and one interval more


def test_the_shipped_file_starts_on_a_day_and_names_the_file_before_it():
    config = load_config(BENCH)
    assert (config.starts, config.previous) == (NEW_DAY, "bench-0.1.yaml")
    assert config.on(NEW_DAY) is config
    assert config.on(NEW_DAY + dt.timedelta(days=400)) is config
    before = config.on(NEW_DAY - dt.timedelta(days=1))
    assert before is config.on(DAY) and before is not config
    assert before.methodology_version == "0.1" and before.schedule.runs_per_day == 4
    assert set(before.pools) == {"core", "wide", "page2"}
    assert before.repeats is None and before.limits.stagger_providers is False
    assert before.repeats_per_day == 0 and before.repeat_ticks == 0


# --- schedule ---------------------------------------------------------------------------------


def test_runs_are_evenly_spaced_and_rotate_by_the_day():
    schedule = Schedule(runs_per_day=4, minute=17, rotate_hours=True)
    day = dt.date(2026, 11, 12)
    hours = [schedule.due_hour(day, k) for k in range(4)]
    assert [b - a for a, b in zip(hours, hours[1:], strict=False)] == [6, 6, 6]
    assert schedule.due_time(day, 0).endswith(":17")
    next_hours = [schedule.due_hour(day + dt.timedelta(days=1), k) for k in range(4)]
    assert next_hours != hours


def test_every_hour_of_the_day_is_sampled_within_six_days():
    schedule = Schedule(runs_per_day=4)
    hours = {schedule.due_hour(DAY + dt.timedelta(days=d), k) for d in range(6) for k in range(4)}
    assert hours == set(range(24))


def test_without_rotation_the_hours_are_fixed():
    schedule = Schedule(runs_per_day=4, rotate_hours=False)
    for offset in range(3):
        day = DAY + dt.timedelta(days=offset)
        assert [schedule.due_hour(day, k) for k in range(4)] == [0, 6, 12, 18]


def test_the_run_due_in_an_hour():
    schedule = Schedule(runs_per_day=4, rotate_hours=False)
    assert schedule.run_due_in_hour(dt.datetime(2026, 11, 12, 6, 40, tzinfo=dt.UTC)) == 1
    assert schedule.run_due_in_hour(dt.datetime(2026, 11, 12, 7, 0, tzinfo=dt.UTC)) is None


def test_a_run_is_owed_for_the_gap_between_runs_less_an_hour():
    """Long enough to outlast the hours in which GitHub starts nothing, and over before
    the following run falls due."""

    def hours(runs: int) -> float:
        return Schedule(runs_per_day=runs).grace / dt.timedelta(hours=1)

    assert hours(3) == 7
    assert hours(4) == 5
    assert hours(8) == 2
    assert hours(12) == 1  # half the gap, where an hour less would leave nothing
    assert hours(24) == 0.5
    for runs in (1, 2, 3, 4, 6, 8, 12, 24):
        schedule = Schedule(runs_per_day=runs)
        assert dt.timedelta(0) < schedule.grace < dt.timedelta(hours=schedule.step_hours)


def test_runs_per_day_must_divide_the_day(tmp_path):
    data = base()
    data["schedule"]["runs_per_day"] = 5
    assert "must divide 24" in error(tmp_path, data)


# --- pools and limits ---------------------------------------------------------------------------


def test_a_pool_needs_exactly_one_origin(tmp_path):
    data = base()
    data["pools"]["mobile"]["source"] = "queries/core.yaml"
    assert "exactly one of `source`, `from` or `follows`" in error(tmp_path, data)
    assert "exactly one of `source`, `from` or `follows`" in error(tmp_path, with_pool(per_day=4))
    both = with_pool(**{"from": "core", "follows": "core", "per_run": 1})
    assert "exactly one of `source`, `from` or `follows`" in error(tmp_path, both)


def test_from_must_name_a_pool_with_a_source(tmp_path):
    data = base()
    data["pools"]["mobile"]["from"] = "nowhere"
    assert "pool 'mobile' is `from: nowhere`, which is not a pool" in error(tmp_path, data)
    borrowed = with_pool(**{"from": "mobile", "per_day": 4})
    assert "has no `source` of its own" in error(tmp_path, borrowed)


def test_follows_must_name_a_pool_with_a_source(tmp_path):
    lost = with_pool(follows="nowhere", per_run=1)
    assert "pool 'extra' is `follows: nowhere`, which is not a pool" in error(tmp_path, lost)
    assert "has no `source` of its own" in error(tmp_path, with_pool(follows="mobile", per_run=1))


def test_a_pool_with_a_list_counts_per_day_and_one_that_follows_counts_per_run(tmp_path):
    follows = with_pool(follows="core", per_day=3)
    assert "a pool with `follows` needs `per_run`, not `per_day`" in error(tmp_path, follows)
    assert "needs `per_run`, not `per_day`" in error(tmp_path, with_pool(follows="core"))
    borrows = with_pool(**{"from": "core", "per_run": 1})
    assert "a pool with `source` or `from` needs `per_day`, not `per_run`" in error(
        tmp_path, borrows
    )


def test_a_pool_that_follows_runs_in_the_runs_of_the_pool_it_follows(tmp_path):
    """Its searches are searches that pool sends in the same run: there must be some."""
    data = with_pool(follows="core", per_run=1, runs=[0])
    assert "pool 'extra' must run in the same runs as 'core'" in error(tmp_path, data)
    data["pools"]["extra"]["runs"] = [0, 1, 2]
    assert load_config(write(tmp_path, data)).pool_runs("extra") == [0, 1, 2]


def test_later_pages_need_a_first_page_to_follow(tmp_path):
    alone = with_pool(**{"from": "core", "per_day": 4, "pages": [2, 3]})
    assert "`pages` needs `follows`: later pages follow a first page" in error(tmp_path, alone)
    both = with_pool(follows="core", per_run=1, page=2, pages=[2, 3])
    assert "give `page` or `pages`, not both" in error(tmp_path, both)


@pytest.mark.parametrize("pages", [[], [1, 2], [2, 11]])
def test_pages_are_later_pages_up_to_the_tenth(tmp_path, pages):
    data = with_pool(follows="core", per_run=1, pages=pages)
    assert "`pages` lists pages from 2 to 10" in error(tmp_path, data)


@pytest.mark.parametrize("pages", [[3, 2], [2, 2, 3]])
def test_pages_are_in_order_and_each_page_once(tmp_path, pages):
    data = with_pool(follows="core", per_run=1, pages=pages)
    assert "`pages` must be ascending, each page once" in error(tmp_path, data)


@pytest.mark.parametrize(
    "special",
    [
        {"follows": "core", "per_run": 1},
        {"follows": "core", "per_run": 1, "pages": [2, 3]},
        {"from": "core", "per_day": 4, "time_range": "week"},
        {"from": "core", "per_day": 4, "feature": "city"},
    ],
    ids=["follows", "pages", "time_range", "feature"],
)
def test_a_headline_pool_is_plain_first_pages_of_its_own_list(tmp_path, special):
    message = error(tmp_path, with_pool(headline=True, **special))
    assert "a headline pool is plain first pages of its own list" in message
    assert load_config(write(tmp_path, with_pool(**special))).pools["extra"].headline is False


def test_the_only_time_range_is_the_past_week(tmp_path):
    data = with_pool(**{"from": "core", "per_day": 4, "time_range": "month"})
    assert "pools.extra.time_range" in error(tmp_path, data)


@pytest.mark.parametrize("name", ["warmup", "repeat"])
def test_the_runners_own_pool_names_cannot_be_taken(tmp_path, name):
    data = base()
    data["pools"][name] = {"from": "core", "per_day": 4}
    assert f"pool name '{name}' is taken by the runner" in error(tmp_path, data)


def test_a_pool_that_follows_sends_one_task_for_each_page_of_each_search(tmp_path):
    data = with_pool(follows="core", per_run=2, pages=[2, 3, 4])
    config = load_config(write(tmp_path, data))
    assert config.pools["extra"].tasks_per_search == 3
    assert config.tasks_per_day("extra") == 2 * 3 * config.schedule.runs_per_day
    assert config.tasks_per_day("mobile") == 8  # a pool with a list: what the file says
    assert config.source_of("extra") == config.source_of("core")


def test_pool_runs_must_exist_in_the_schedule(tmp_path):
    data = base()
    beyond = data["schedule"]["runs_per_day"]  # the first index the schedule does not have
    data["pools"]["mobile"]["runs"] = [0, beyond]
    assert f"names runs [{beyond}]" in error(tmp_path, data)
    data["pools"]["mobile"]["runs"] = []
    assert "empty list of runs" in error(tmp_path, data)


def test_errors_name_the_place_and_the_problem(tmp_path):
    data = base()
    data["pools"]["core"]["per_day"] = 0
    data["limits"]["speed"] = "fast"
    message = error(tmp_path, data)
    assert "pools.core.per_day: Input should be greater than 0" in message
    assert "limits.speed: Extra inputs are not permitted" in message


def test_a_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="pass --config"):
        load_config(tmp_path / "bench.yaml")


def test_backpressure_is_possible_only_when_the_timeout_outruns_the_cap():
    assert not Limits().backpressure_possible()  # 60 s / 15 s = 4 waiting, cap 5
    # exactly timeout / interval is not enough: the oldest request is timing out at the
    # very moment the next task is due
    assert Limits(max_in_flight_per_provider=4).backpressure_possible()
    assert Limits(dispatch_interval_seconds=10).backpressure_possible()
    assert Limits().backpressure_possible(interval=0)


def test_providers_are_asked_at_the_same_instant_unless_the_file_says_otherwise():
    assert Limits().stagger_providers is False


# --- repeats ------------------------------------------------------------------------------------


def test_repeats_per_day_are_those_of_a_run_in_every_run(tmp_path):
    data = base()
    data["repeats"] = {"after_seconds": 60, "first_pages": 4, "later_pages": 1}
    config = load_config(write(tmp_path, data))
    assert config.repeats.per_run == 5
    assert config.repeats_per_day == 5 * config.schedule.runs_per_day
    del data["repeats"]
    assert load_config(write(tmp_path, data)).repeats_per_day == 0


def test_a_repeat_falls_a_whole_number_of_dispatch_intervals_later(tmp_path):
    data = base()
    data["repeats"] = {"after_seconds": 100, "first_pages": 1}
    message = error(tmp_path, data)
    assert "repeats.after_seconds must be a whole number of dispatch intervals (15 s)" in message
    data["repeats"]["after_seconds"] = 90
    assert load_config(write(tmp_path, data)).repeat_ticks == 6


def test_repeats_of_later_pages_need_a_pool_with_pages(tmp_path):
    data = base()
    del data["pools"]["deep"]
    assert "repeats.later_pages needs a pool with `pages`" in error(tmp_path, data)
    data["repeats"]["later_pages"] = 0
    assert load_config(write(tmp_path, data)).repeats.per_run == data["repeats"]["first_pages"]


def test_two_pages_of_one_search_are_more_than_a_time_limit_apart(tmp_path):
    """The next page is never asked for while the page before it may still be on its way."""
    data = base()
    data["limits"] |= {"timeout_seconds": 60, "dispatch_interval_seconds": 15}
    assert load_config(write(tmp_path, data)).page_gap_ticks == 5
    data["limits"] |= {"timeout_seconds": 50, "max_in_flight_per_provider": 6}
    config = load_config(write(tmp_path, data))
    assert config.page_gap_ticks == 5  # 50 s is still inside the fourth interval
    assert config.page_gap_ticks * 15 > 50


# --- one file after another -----------------------------------------------------------------------


def two_files(tmp_path, starts: dt.date = dt.date(2026, 6, 10)):
    """A configuration in force from `starts`, and the one before it."""
    before = base()
    before["methodology_version"] = "before"
    write(tmp_path, before, "before.yaml")
    return load_config(write(tmp_path, base() | {"starts": starts, "previous": "before.yaml"}))


def test_a_day_before_the_start_has_the_configuration_before(tmp_path):
    config = two_files(tmp_path)
    assert config.on(dt.date(2026, 6, 9)).methodology_version == "before"
    assert config.on(dt.date(2020, 1, 1)).methodology_version == "before"


def test_the_day_of_the_start_and_every_later_day_have_the_file_itself(tmp_path):
    config = two_files(tmp_path)
    assert config.on(dt.date(2026, 6, 10)) is config
    assert config.on(dt.date(2027, 1, 1)) is config


def test_a_file_without_a_start_is_in_force_on_every_day(tmp_path):
    config = load_config(write(tmp_path, base()))
    assert config.on(dt.date(2000, 1, 1)) is config


def test_a_day_long_ago_is_found_through_every_file_in_between(tmp_path):
    oldest = base() | {"methodology_version": "oldest"}
    write(tmp_path, oldest, "oldest.yaml")
    middle = base() | {"methodology_version": "middle"}
    middle |= {"starts": dt.date(2026, 3, 1), "previous": "oldest.yaml"}
    write(tmp_path, middle, "middle.yaml")
    newest = base() | {"starts": dt.date(2026, 6, 10), "previous": "middle.yaml"}
    config = load_config(write(tmp_path, newest))
    versions = [config.on(dt.date(2026, month, 1)).methodology_version for month in (2, 4, 7)]
    assert versions == ["oldest", "middle", config.methodology_version]


def test_a_start_and_the_file_before_it_come_together(tmp_path):
    for alone in ({"starts": dt.date(2026, 6, 10)}, {"previous": "before.yaml"}):
        assert "`starts` and `previous` go together" in error(tmp_path, base() | alone)


def test_the_file_before_cannot_be_the_file_itself(tmp_path):
    data = base() | {"starts": dt.date(2026, 6, 10), "previous": "bench.yaml"}
    assert "`previous` names the file itself" in error(tmp_path, data)


def test_a_missing_file_before_is_named(tmp_path):
    data = base() | {"starts": dt.date(2026, 6, 10), "previous": "lost.yaml"}
    message = error(tmp_path, data)
    assert "lost.yaml: not found" in message


# --- the configuration hash ----------------------------------------------------------------------


def test_config_hash_follows_the_configuration_and_the_queries(tmp_path):
    shutil.copytree(ROOT / "queries", tmp_path / "queries")
    data = base()
    first = config_hash(load_config(write(tmp_path, data)), tmp_path)
    assert first == config_hash(load_config(write(tmp_path, data)), tmp_path)
    assert len(first) == 12

    data["pools"]["core"]["per_day"] += 1  # whatever the file says today
    changed = config_hash(load_config(write(tmp_path, data)), tmp_path)
    assert changed != first

    queries = tmp_path / load_config(write(tmp_path, data)).source_of("core")
    queries.write_text(queries.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert config_hash(load_config(write(tmp_path, data)), tmp_path) != changed


def test_the_configuration_before_keeps_the_hash_in_its_published_records():
    """Records of methodology 0.1 carry this hash. The settings that came after it are left
    out of the hash while a file does not use them, so the old file still hashes to it."""
    assert config_hash(load_config(ROOT / "bench-0.1.yaml"), ROOT) == "72622f54b76b"
    assert config_hash(load_config(BENCH).on(DAY), ROOT) == "72622f54b76b"
    assert config_hash(load_config(BENCH), ROOT) != "72622f54b76b"


def test_the_start_and_the_file_before_do_not_change_the_hash(tmp_path):
    """They say when a file is in force, not what it measures."""
    shutil.copytree(ROOT / "queries", tmp_path / "queries")
    alone = config_hash(load_config(write(tmp_path, base())), tmp_path)
    assert config_hash(two_files(tmp_path), tmp_path) == alone
    assert config_hash(two_files(tmp_path, dt.date(2026, 9, 1)), tmp_path) == alone


def _stagger(data: dict) -> None:
    data["limits"]["stagger_providers"] = not data["limits"]["stagger_providers"]


def _later(data: dict) -> None:
    data["repeats"]["after_seconds"] += 15


def _no_repeats(data: dict) -> None:
    data["repeats"] = {"after_seconds": 300, "first_pages": 0, "later_pages": 0}


def _fewer_pages(data: dict) -> None:
    data["pools"]["deep"]["pages"] = [2, 3]


def _any_time(data: dict) -> None:
    del data["pools"]["week"]["time_range"]


def _another_feature(data: dict) -> None:
    data["pools"]["city"]["feature"] = "place"


@pytest.mark.parametrize(
    "change", [_stagger, _later, _no_repeats, _fewer_pages, _any_time, _another_feature]
)
def test_what_changes_the_timetable_or_the_searches_changes_the_hash(tmp_path, change):
    shutil.copytree(ROOT / "queries", tmp_path / "queries")
    data = base()
    first = config_hash(load_config(write(tmp_path, data)), tmp_path)
    change(data)
    assert config_hash(load_config(write(tmp_path, data)), tmp_path) != first
