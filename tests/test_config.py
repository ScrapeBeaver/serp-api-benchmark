"""bench.yaml: the schedule, the pools and readable errors."""

from __future__ import annotations

import datetime as dt
import shutil

import pytest
import yaml
from conftest import DAY, ROOT

from serp_bench.config import ConfigError, Limits, Schedule, config_hash, load_config


def write(tmp_path, data: dict):
    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def base() -> dict:
    """The shipped configuration, with a mobile pool added: the tests below need a second
    pool that borrows its queries, whatever pools the shipped file has today."""
    data = yaml.safe_load((ROOT / "bench.yaml").read_text(encoding="utf-8"))
    data["pools"]["mobile"] = {"from": "core", "per_day": 8, "device": "mobile", "headline": False}
    return data


def error(tmp_path, data: dict) -> str:
    with pytest.raises(ConfigError) as caught:
        load_config(write(tmp_path, data))
    return str(caught.value)


def test_the_shipped_configuration_loads():
    config = load_config(ROOT / "bench.yaml")
    assert set(config.pools) == {"core", "page2"}
    assert config.pools["core"].headline and not config.pools["page2"].headline
    assert config.source_of("page2") == "queries/smoke.yaml"
    assert config.pool_runs("core") == list(range(config.schedule.runs_per_day))  # every run


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


def test_runs_per_day_must_divide_the_day(tmp_path):
    data = base()
    data["schedule"]["runs_per_day"] = 5
    assert "must divide 24" in error(tmp_path, data)


# --- pools and limits ---------------------------------------------------------------------------


def test_a_pool_needs_exactly_one_origin(tmp_path):
    data = base()
    data["pools"]["mobile"]["source"] = "queries/smoke.yaml"
    assert "exactly one of `source` or `from`" in error(tmp_path, data)


def test_from_must_name_a_pool_with_a_source(tmp_path):
    data = base()
    data["pools"]["mobile"]["from"] = "nowhere"
    assert "which is not a pool" in error(tmp_path, data)
    data = base()
    data["pools"]["page2"]["from"] = "mobile"
    assert "has no `source` of its own" in error(tmp_path, data)


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

    queries = tmp_path / "queries" / "smoke.yaml"
    queries.write_text(queries.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert config_hash(load_config(write(tmp_path, data)), tmp_path) != changed
