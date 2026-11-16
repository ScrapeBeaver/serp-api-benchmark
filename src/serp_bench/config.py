"""`bench.yaml`: which providers are measured, when, and how many requests of each kind.

A pool is a named stream of tasks: where its queries come from, how many are sent per day,
in which of the day's slots, and with which device and results page. Changing the pools
changes what the numbers mean, so every record carries a hash of the effective
configuration and reports never silently mix two configurations.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from serp_bench.models import Device


class ConfigError(Exception):
    """A configuration problem, phrased for the person editing the file."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Schedule(_Strict):
    """When runs are due. `runs_per_day` runs, evenly spaced, at `minute` past the hour.

    With `rotate_hours` the whole timetable moves one hour later each day and wraps, so
    over 24 / runs_per_day days every hour of the day has been sampled. Without it the
    benchmark would only ever see the same few hours.
    """

    runs_per_day: int = Field(default=4, gt=0)
    minute: int = Field(default=17, ge=0, le=59)  # off the hour: scheduled CI is busiest at :00
    rotate_hours: bool = True

    @model_validator(mode="after")
    def _divides_the_day(self) -> Schedule:
        if 24 % self.runs_per_day != 0:
            raise ValueError("runs_per_day must divide 24 (1, 2, 3, 4, 6, 8, 12 or 24)")
        return self

    @property
    def step_hours(self) -> int:
        return 24 // self.runs_per_day

    def due_hour(self, day: dt.date, run_index: int) -> int:
        """The UTC hour at which run `run_index` of `day` is due."""
        if not 0 <= run_index < self.runs_per_day:
            raise ValueError(f"run index {run_index} is not in 0..{self.runs_per_day - 1}")
        shift = day.toordinal() % self.step_hours if self.rotate_hours else 0
        return run_index * self.step_hours + shift

    def due_time(self, day: dt.date, run_index: int) -> str:
        return f"{self.due_hour(day, run_index):02d}:{self.minute:02d}"

    def run_due_in_hour(self, now: dt.datetime) -> int | None:
        """The run that is due in the UTC hour of `now`, or None."""
        for run_index in range(self.runs_per_day):
            if self.due_hour(now.date(), run_index) == now.hour:
                return run_index
        return None

    def due_at(self, day: dt.date, run_index: int) -> dt.datetime:
        """The UTC moment at which run `run_index` of `day` is due."""
        hour = self.due_hour(day, run_index)
        return dt.datetime.combine(day, dt.time(hour, self.minute), tzinfo=dt.UTC)

    @property
    def grace(self) -> dt.timedelta:
        """How long after its due time a run is still done by the schedule: the gap between
        two runs less an hour, and never less than half the gap. Five hours at four runs
        a day.

        A scheduled job on GitHub does not only start late, some of the hourly starts never
        happen: on 2026-11-16 there were six in twelve hours, with 3 h 24 min between two
        of them. A run is owed for long enough to outlast such a silence, and stops being
        owed an hour before the following run falls due, so the two never overlap."""
        gap = dt.timedelta(hours=self.step_hours)
        return max(gap - dt.timedelta(hours=1), gap / 2)


class Pool(_Strict):
    source: str | None = None  # a query file, relative to bench.yaml
    from_: str | None = Field(default=None, alias="from")  # reuse another pool's queries
    per_day: int = Field(gt=0)
    runs: Literal["all"] | list[int] = "all"  # which of the day's runs, by index from 0
    device: Device = Device.desktop
    page: int = Field(default=1, ge=1, le=10)
    headline: bool = False  # counts towards the main table

    @model_validator(mode="after")
    def _one_origin(self) -> Pool:
        if (self.source is None) == (self.from_ is None):
            raise ValueError("a pool needs exactly one of `source` or `from`")
        return self


class Limits(_Strict):
    timeout_seconds: float = Field(default=60, gt=0)
    # Tasks are sent on a fixed timetable, one every `dispatch_interval_seconds`, whether
    # or not earlier answers have arrived.
    dispatch_interval_seconds: float = Field(default=15, ge=0)
    # Requests to one provider may overlap up to this many. A task that would exceed it is
    # not sent to that provider and is recorded as void (client_backpressure). Skipping a
    # task because a provider is slow would under-count exactly its slow period, so the cap
    # must be out of reach: one more than the requests a full time limit can hold.
    max_in_flight_per_provider: int = Field(default=5, gt=0)
    max_tasks_per_provider_per_run: int = Field(default=110, gt=0)
    # After the timetable, a first page that matched no other provider's page is asked for
    # once more from the same provider (step 3, the control repeat). At most this many per
    # provider per run; 0 switches the repeats off. They count towards the limit above.
    max_control_repeats_per_provider_per_run: int = Field(default=0, ge=0)

    def backpressure_possible(self, interval: float | None = None) -> bool:
        """True when a provider that always uses the whole time limit could hit the cap.

        With one task every `interval` seconds, at most ceil(timeout / interval) requests
        are still waiting when the next is due, and the oldest of them is timing out at
        that very moment. One slot of headroom keeps that race from voiding a task.
        """
        interval = self.dispatch_interval_seconds if interval is None else interval
        if interval <= 0:
            return True
        needed = math.ceil(self.timeout_seconds / interval) + 1
        return needed > self.max_in_flight_per_provider


class Budget(_Strict):
    # The cheapest published plan that covers a month of this file's volume must not cost
    # more than this, for any provider. `serp-bench budget` checks it. None: no ceiling.
    max_usd_per_provider_per_month: float | None = Field(default=None, gt=0)


class Site(_Strict):
    """The results page. None of this changes what is measured."""

    url: str = "https://benchmark.scrapebeaver.com"  # where the page is published
    repository: str | None = None  # the public repository, linked from the page
    operator: str | None = None  # the provider run by whoever runs this benchmark
    # Before launch: every measured provider is shown whatever its status, the page says it
    # is a preview, and search engines are asked not to index it.
    preview: bool = True


class BenchConfig(_Strict):
    methodology_version: str
    providers: list[str] = Field(min_length=1)
    schedule: Schedule
    pools: dict[str, Pool] = Field(min_length=1)
    limits: Limits = Field(default_factory=Limits)
    budget: Budget = Field(default_factory=Budget)
    site: Site = Field(default_factory=Site)

    @model_validator(mode="after")
    def _consistent(self) -> BenchConfig:
        last = self.schedule.runs_per_day - 1
        for name, pool in self.pools.items():
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise ValueError(f"pool name '{name}' must be lower case letters, digits, _")
            if pool.runs != "all":
                if not pool.runs:
                    raise ValueError(f"pool '{name}' has an empty list of runs")
                outside = [index for index in pool.runs if not 0 <= index <= last]
                if outside:
                    raise ValueError(
                        f"pool '{name}' names runs {outside}; the schedule has runs 0..{last}"
                    )
                if len(set(pool.runs)) != len(pool.runs):
                    raise ValueError(f"pool '{name}' names a run twice")
            if pool.from_ is not None:
                origin = self.pools.get(pool.from_)
                if origin is None:
                    raise ValueError(f"pool '{name}' is `from: {pool.from_}`, which is not a pool")
                if origin.source is None:
                    raise ValueError(
                        f"pool '{name}' is `from: {pool.from_}`, which has no `source` of its own"
                    )
        return self

    def pool_runs(self, name: str) -> list[int]:
        """The run indexes a pool takes part in, ascending."""
        pool = self.pools[name]
        if pool.runs == "all":
            return list(range(self.schedule.runs_per_day))
        return sorted(pool.runs)

    def source_of(self, name: str) -> str:
        """The query file a pool draws from, following `from`."""
        pool = self.pools[name]
        if pool.source is not None:
            return pool.source
        origin = self.pools[pool.from_]  # type: ignore[index]
        assert origin.source is not None
        return origin.source


def load_config(path: Path) -> BenchConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path}: not found (run from the project folder or pass --config)"
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    try:
        return BenchConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}:\n{_format(exc)}") from exc


def _format(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(top level)"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {where}: {message}")
    return "\n".join(lines)


def config_hash(config: BenchConfig, root: Path) -> str:
    """A short hash of the effective configuration: the file's meaning plus the contents of
    every query file it draws from. Two runs with the same hash are comparable.

    The budget ceiling and the page settings are left out: they decide what we may spend
    and how results are shown, not what is measured."""
    sources = sorted({config.source_of(name) for name in config.pools})
    digest = hashlib.sha256()
    measured = config.model_dump(mode="json", by_alias=True, exclude={"budget", "site"})
    digest.update(json.dumps(measured, sort_keys=True).encode())
    for source in sources:
        digest.update(b"\0" + source.encode() + b"\0")
        digest.update((root / source).read_bytes())
    return digest.hexdigest()[:12]
