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
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

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
        two runs less an hour, and never less than half the gap. Seven hours at three runs
        a day, five at four.

        A scheduled job on GitHub does not only start late, some of the hourly starts never
        happen: on 2026-11-16 there were six in twelve hours, with 3 h 24 min between two
        of them. A run is owed for long enough to outlast such a silence, and stops being
        owed an hour before the following run falls due, so the two never overlap."""
        gap = dt.timedelta(hours=self.step_hours)
        return max(gap - dt.timedelta(hours=1), gap / 2)


class Pool(_Strict):
    """A stream of tasks. Its searches come from a list of its own (`source`), from another
    pool's list (`from`), or from the searches another pool sends in the very same run
    (`follows`): the later pages of a search need its first page in the same run, and a
    filtered search is read against the same search without the filter."""

    source: str | None = None  # a query file, relative to bench.yaml
    from_: str | None = Field(default=None, alias="from")  # reuse another pool's queries
    follows: str | None = None  # draw from what that pool sends in the same run
    per_day: int | None = Field(default=None, gt=0)  # with `source` or `from`
    per_run: int | None = Field(default=None, gt=0)  # with `follows`: searches per run
    runs: Literal["all"] | list[int] = "all"  # which of the day's runs, by index from 0
    device: Device = Device.desktop
    page: int = Field(default=1, ge=1, le=10)
    # With `follows`: one task for each of these pages of the search, sent in order after
    # its first page and never two at a time, the way a person turns pages.
    pages: list[int] | None = None
    time_range: Literal["week"] | None = None  # only results of the past week
    feature: str | None = None  # the feature this pool measures; shown apart on the page
    headline: bool = False  # counts towards the main table

    @model_validator(mode="after")
    def _one_origin(self) -> Pool:
        origins = [self.source, self.from_, self.follows]
        if sum(origin is not None for origin in origins) != 1:
            raise ValueError("a pool needs exactly one of `source`, `from` or `follows`")
        if self.follows is None:
            if self.per_day is None or self.per_run is not None:
                raise ValueError("a pool with `source` or `from` needs `per_day`, not `per_run`")
            if self.pages is not None:
                raise ValueError("`pages` needs `follows`: later pages follow a first page")
        elif self.per_run is None or self.per_day is not None:
            raise ValueError("a pool with `follows` needs `per_run`, not `per_day`")
        if self.pages is not None:
            if self.page != 1:
                raise ValueError("give `page` or `pages`, not both")
            if not self.pages or any(not 2 <= page <= 10 for page in self.pages):
                raise ValueError("`pages` lists pages from 2 to 10")
            if sorted(set(self.pages)) != self.pages:
                raise ValueError("`pages` must be ascending, each page once")
        special = self.follows or self.pages or self.time_range or self.feature
        if self.headline and special:
            raise ValueError(
                "a headline pool is plain first pages of its own list: no `follows`, "
                "`pages`, `time_range` or `feature`"
            )
        return self

    @property
    def tasks_per_search(self) -> int:
        return len(self.pages) if self.pages else 1


class Repeats(_Strict):
    """The same search asked again from the same provider, a fixed time after the first
    answer was asked for. It is what the repeatability figure is computed from.

    Each run, `first_pages` of its headline searches are asked again, and `later_pages` of
    the pages of its paged search (which pages alternates from run to run)."""

    after_seconds: float = Field(default=300, gt=0)
    first_pages: int = Field(default=0, ge=0)
    later_pages: int = Field(default=0, ge=0)

    @property
    def per_run(self) -> int:
        return self.first_pages + self.later_pages


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
    # False: every provider receives a task at the same instant. True: one provider after
    # another, one dispatch interval apart, in an order drawn anew for every run. A provider
    # still receives one task per interval; no two providers ask Google the same thing in
    # the same second, and none is always first.
    stagger_providers: bool = False

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


RESERVED_POOL_NAMES = frozenset({"warmup", "repeat"})


class BenchConfig(_Strict):
    methodology_version: str
    # This file is in force from 00:00 UTC of `starts`. Before that day the file named by
    # `previous` is: what a day's runs are does not change in the middle of the day.
    starts: dt.date | None = None
    previous: str | None = None  # an earlier bench.yaml, relative to this one
    providers: list[str] = Field(min_length=1)
    schedule: Schedule
    pools: dict[str, Pool] = Field(min_length=1)
    repeats: Repeats | None = None
    limits: Limits = Field(default_factory=Limits)
    budget: Budget = Field(default_factory=Budget)
    site: Site = Field(default_factory=Site)
    _before: BenchConfig | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _consistent(self) -> BenchConfig:
        if (self.starts is None) != (self.previous is None):
            raise ValueError("`starts` and `previous` go together: a day, and the file before it")
        last = self.schedule.runs_per_day - 1
        for name, pool in self.pools.items():
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise ValueError(f"pool name '{name}' must be lower case letters, digits, _")
            if name in RESERVED_POOL_NAMES:
                raise ValueError(f"pool name '{name}' is taken by the runner")
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
            for word, target in (("from", pool.from_), ("follows", pool.follows)):
                if target is None:
                    continue
                origin = self.pools.get(target)
                if origin is None:
                    raise ValueError(f"pool '{name}' is `{word}: {target}`, which is not a pool")
                if origin.source is None:
                    raise ValueError(
                        f"pool '{name}' is `{word}: {target}`, which has no `source` of its own"
                    )
            if pool.follows is not None and self.pool_runs(name) != self.pool_runs(pool.follows):
                raise ValueError(f"pool '{name}' must run in the same runs as '{pool.follows}'")
        if self.repeats is not None:
            interval = self.limits.dispatch_interval_seconds
            if interval > 0 and self.repeats.after_seconds % interval:
                raise ValueError(
                    "repeats.after_seconds must be a whole number of dispatch intervals "
                    f"({interval:g} s)"
                )
            if self.repeats.later_pages and not any(p.pages for p in self.pools.values()):
                raise ValueError("repeats.later_pages needs a pool with `pages`")
        return self

    def on(self, day: dt.date) -> BenchConfig:
        """The configuration in force on `day`: this one, or the one before it."""
        if self.starts is not None and day < self.starts and self._before is not None:
            return self._before.on(day)
        return self

    def pool_runs(self, name: str) -> list[int]:
        """The run indexes a pool takes part in, ascending."""
        pool = self.pools[name]
        if pool.runs == "all":
            return list(range(self.schedule.runs_per_day))
        return sorted(pool.runs)

    def source_of(self, name: str) -> str:
        """The query file a pool draws from, following `from` and `follows`."""
        pool = self.pools[name]
        if pool.source is not None:
            return pool.source
        origin = self.pools[pool.from_ or pool.follows]  # type: ignore[index]
        assert origin.source is not None
        return origin.source

    def tasks_per_day(self, name: str) -> int:
        """How many tasks a pool sends in a day."""
        pool = self.pools[name]
        if pool.per_run is not None:
            return pool.per_run * pool.tasks_per_search * len(self.pool_runs(name))
        assert pool.per_day is not None
        return pool.per_day

    @property
    def repeats_per_day(self) -> int:
        return self.repeats.per_run * self.schedule.runs_per_day if self.repeats else 0

    @property
    def repeat_ticks(self) -> int:
        """How many dispatch intervals after a search its repeat is sent."""
        if self.repeats is None:
            return 0
        interval = self.limits.dispatch_interval_seconds
        return max(1, round(self.repeats.after_seconds / interval)) if interval > 0 else 1

    @property
    def page_gap_ticks(self) -> int:
        """Dispatch intervals between two pages of one search: more than a time limit, so
        the next page is never asked for while the page before it may still be on its way."""
        interval = self.limits.dispatch_interval_seconds
        return math.ceil(self.limits.timeout_seconds / interval) + 1 if interval > 0 else 1


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
        config = BenchConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}:\n{_format(exc)}") from exc
    if config.previous is not None:
        before = (path.parent / config.previous).resolve()
        if before == path.resolve():
            raise ConfigError(f"{path}: `previous` names the file itself")
        config._before = load_config(before)
    return config


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
    measured = config.model_dump(
        mode="json", by_alias=True, exclude={"budget", "site", "starts", "previous"}
    )
    # Settings added on 2026-11-25 are left out while they are not used, so a configuration
    # written before them keeps the hash it always had.
    if measured["repeats"] is None:
        del measured["repeats"]
    if not measured["limits"]["stagger_providers"]:
        del measured["limits"]["stagger_providers"]
    for pool in measured["pools"].values():
        for key in ("follows", "per_run", "pages", "time_range", "feature"):
            if pool[key] is None:
                del pool[key]
    digest.update(json.dumps(measured, sort_keys=True).encode())
    for source in sources:
        digest.update(b"\0" + source.encode() + b"\0")
        digest.update((root / source).read_bytes())
    return digest.hexdigest()[:12]
