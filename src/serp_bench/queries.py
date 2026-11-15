"""Query sets, and the planner that turns (date, slot, configuration) into a task list.

Planning is deterministic: the same date, slot, configuration and query files always give
the same tasks in the same order, on any machine. Anyone can recompute what a run was
supposed to send.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from serp_bench.config import BenchConfig, ConfigError
from serp_bench.models import Task


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    query: str = Field(min_length=1, max_length=500)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    locale: str = Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    category: str | None = None


class QuerySet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    description: str | None = None
    queries: list[Query] = Field(min_length=1)


def load_query_set(path: Path) -> list[Query]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: query file not found") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    try:
        query_set = QuerySet.model_validate(data)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
        raise ConfigError(f"{path}:\n" + "\n".join(lines)) from exc
    ids = [q.id for q in query_set.queries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate query ids: {duplicates}")
    return query_set.queries


# Day 0 of every pool's sequence. Any fixed date works; it only anchors the passes.
_EPOCH = dt.date(2026, 1, 1).toordinal()


def _rng(*parts: object) -> random.Random:
    """A random generator seeded from the given parts, stable across machines and versions."""
    text = "\x1f".join(str(part) for part in parts)
    return random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))


class _PassSequence:
    """A pool's endless sequence of queries: pass after pass over the whole list.
    Position p is query `p mod n` of pass `p div n`.

    The list is cut once into day blocks of `per_day` queries, in an order shuffled for
    the pool. A query keeps its block on every pass, so when `per_day` divides the list it
    is sent exactly every `n / per_day` days: a per-query history needs that regularity.
    Inside its block the order is shuffled anew on every pass, so a query does not stay in
    the same run of its day.
    """

    def __init__(self, queries: list[Query], per_day: int, seed_parts: tuple[object, ...]) -> None:
        base = list(queries)
        _rng("blocks", *seed_parts).shuffle(base)
        size = max(1, min(per_day, len(base)))
        self._blocks = [base[start : start + size] for start in range(0, len(base), size)]
        self._length = len(base)
        self._seed_parts = seed_parts
        self._passes: dict[int, list[Query]] = {}

    def at(self, position: int) -> Query:
        number, within = divmod(position, self._length)
        if number not in self._passes:
            ordered: list[Query] = []
            for index, block in enumerate(self._blocks):
                block = list(block)
                _rng("pass", number, index, *self._seed_parts).shuffle(block)
                ordered += block
            self._passes[number] = ordered
        return self._passes[number][within]


def per_run_counts(per_day: int, run_count: int) -> list[int]:
    """`per_day` spread as evenly as possible over the runs; earlier runs take the rest."""
    base, rest = divmod(per_day, run_count)
    return [base + (1 if index < rest else 0) for index in range(run_count)]


def stored_runs(config: BenchConfig, day: dt.date, stored: Path | None) -> set[int]:
    """The indexes of the day's runs that already have a record file under `stored`
    (a folder laid out like the data branch: YYYY/MM/DD/run-HHMM.jsonl.gz)."""
    if stored is None:
        return set()
    folder = stored / f"{day:%Y/%m/%d}"
    return {
        index
        for index in range(config.schedule.runs_per_day)
        if any(
            folder.glob(f"run-{config.schedule.due_time(day, index).replace(':', '')}*.jsonl.gz")
        )
    }


def late_run(
    config: BenchConfig, now: dt.datetime, stored: Path | None
) -> tuple[dt.date, int] | None:
    """The run the schedule owes at `now`: the earliest run that fell due within the last
    `schedule.grace` and is not stored yet, with its day. None when there is none.

    A scheduled job does not start on the minute: GitHub starts it tens of minutes late,
    often in the next clock hour, and after midnight for the last run of a day. Asking
    "which run is due in this hour" would then find nothing and the run would be lost.
    """
    schedule = config.schedule
    owed: list[tuple[dt.datetime, dt.date, int]] = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        done = stored_runs(config, day, stored)
        for index in range(schedule.runs_per_day):
            due = schedule.due_at(day, index)
            if index not in done and due <= now <= due + schedule.grace:
                owed.append((due, day, index))
    if not owed:
        return None
    _, day, index = min(owed)
    return day, index


def resolve_run(
    config: BenchConfig,
    run: str,
    now: dt.datetime,
    *,
    day: dt.date | None = None,
    stored: Path | None = None,
) -> int | None:
    """Which of the day's runs to do, from the command line.

    `auto` is the run due in the current UTC hour: what the schedule asks for. `next` is
    the first of the day's runs that is not stored yet: what a person who starts a run by
    hand wants, without having to know which runs were done. A number is that run.
    None when there is nothing to do: no run is due in this hour, or the run is stored
    already (a run is recorded once and never written again).
    """
    day = now.date() if day is None else day
    done = stored_runs(config, day, stored)
    if run == "auto":
        due = config.schedule.run_due_in_hour(now)
        return None if due in done else due
    if run == "next":
        left = [index for index in range(config.schedule.runs_per_day) if index not in done]
        return left[0] if left else None
    last = config.schedule.runs_per_day - 1
    if not re.fullmatch(r"\d+", run) or not 0 <= int(run) <= last:
        raise ConfigError(f"--run '{run}' must be `auto`, `next` or a run index from 0 to {last}")
    return int(run)


def plan(
    config: BenchConfig,
    root: Path,
    day: dt.date,
    run_index: int,
    *,
    seed: int = 0,
) -> list[Task]:
    """The ordered tasks for one run of one day.

    Each pool walks its query list in passes; the next pass starts only when the previous
    one is finished. A day takes the next `per_day` queries of that endless sequence, and a
    run takes its consecutive share of the day's. So every query is sent equally often,
    whatever the sizes:

    * `per_day` equal to the list: each query once a day, in a run that changes daily;
    * `per_day` a quarter of the list: each query exactly every fourth day, never skipped
      for longer, which is what a per-query history needs;
    * `per_day` larger than the list: several passes a day.

    The run's tasks from all pools are then interleaved in an order shuffled for that run.
    """
    if not 0 <= run_index < config.schedule.runs_per_day:
        raise ConfigError(f"run index {run_index} is not in 0..{config.schedule.runs_per_day - 1}")
    version = config.methodology_version
    cache: dict[str, list[Query]] = {}
    tasks: list[Task] = []

    for name, pool in config.pools.items():
        runs = config.pool_runs(name)
        if run_index not in runs:
            continue
        counts = per_run_counts(pool.per_day, len(runs))
        index = runs.index(run_index)
        offset, count = sum(counts[:index]), counts[index]
        if count == 0:
            continue

        source = config.source_of(name)
        if source not in cache:
            cache[source] = load_query_set(root / source)
        sequence = _PassSequence(cache[source], pool.per_day, (name, version, seed))
        day_start = (day.toordinal() - _EPOCH) * pool.per_day
        stamp = f"{day:%Y%m%d}-r{run_index}"
        for position in range(offset, offset + count):
            query = sequence.at(day_start + position)
            tasks.append(
                Task(
                    task_id=f"{stamp}-{name}-{position - offset + 1:03d}",
                    pool=name,
                    headline=pool.headline,
                    query_id=query.id,
                    query=query.query,
                    country=query.country,
                    locale=query.locale,
                    device=pool.device,
                    page=pool.page,
                )
            )

    _rng("run", day.isoformat(), run_index, version, seed).shuffle(tasks)
    return tasks
