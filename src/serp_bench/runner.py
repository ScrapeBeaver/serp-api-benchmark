"""A run: the same tasks sent to every provider at the same moment, on a fixed timetable.

Protocol
* One warm-up request per provider first, so measured requests can reuse an open
  connection. It is recorded with `warmup: true` and never counted.
* Open-loop dispatch. Task i is sent at t0 + i x interval, whether or not earlier answers
  have arrived, and every provider receives it at the same instant. A slow provider
  therefore does not slow the timetable down: if it stalls, it keeps receiving tasks at
  the same rate as everyone else, and its bad period is measured in full rather than
  quietly skipped (the "coordinated omission" trap of waiting for each answer).
* Requests to one provider may overlap up to a cap. A task that would exceed it is not
  sent to that provider and is recorded as void (`client_backpressure`): that is our
  stand's limit, shown in the report, not a failure of the provider.
* No retries, no redirects followed. What a single call returns is what is measured.
* The clock is the runner's, the same for everyone: from sending the request to the
  response headers (`ttfb_ms`) and to the last byte of the body (`total_ms`). How late
  the request left compared with the timetable is recorded as `dispatch_lag_ms`.
* Control repeats last. When every measured request has been answered, each first page
  that matched no other provider's page (`poison.control_candidates`) is asked for once
  more from the same provider. Repeats go one at a time per provider and only after the
  timetable is over, so they can never slow a measured request down. They are recorded
  with `control: true`, serve the step-3 check only, and are never counted.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from serp_bench.models import (
    CLIENT_LAG_THRESHOLD_MS,
    VOID_CLIENT_BACKPRESSURE,
    VOID_NOT_SUPPORTED,
    VOID_RUN_ABORTED,
    Device,
    HttpRequest,
    RawResponse,
    Record,
    Task,
)
from serp_bench.poison import control_candidates
from serp_bench.provider import Provider
from serp_bench.records import RecordWriter
from serp_bench.validate import Step1, judge, not_sent

WARMUP_QUERY = ("warmup", "weather today", "US", "en")

OnRecord = Callable[[Record], None]


@dataclass(frozen=True)
class RunContext:
    run_id: str
    config_hash: str
    methodology_version: str
    run_index: int | None = None


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def send(client: httpx.AsyncClient, request: HttpRequest, timeout: float) -> RawResponse:
    """Send one request and time it. Never raises for anything the network can do."""
    start = time.perf_counter()

    async def exchange() -> tuple[int, dict[str, str], bytes, float]:
        async with client.stream(
            request.method,
            request.url,
            headers=request.headers or None,
            params=request.params or None,
            json=request.json_body,
        ) as response:
            ttfb = time.perf_counter() - start
            body = await response.aread()
            return response.status_code, dict(response.headers), body, ttfb

    def elapsed_ms() -> float:
        return round((time.perf_counter() - start) * 1000, 1)

    try:
        status, headers, body, ttfb = await asyncio.wait_for(exchange(), timeout)
    except (TimeoutError, httpx.TimeoutException) as exc:
        return RawResponse(error="timeout", error_detail=type(exc).__name__, total_ms=elapsed_ms())
    except httpx.HTTPError as exc:
        return RawResponse(error="network", error_detail=type(exc).__name__, total_ms=elapsed_ms())
    return RawResponse(
        status=status,
        headers=headers,
        body=body,
        ttfb_ms=round(ttfb * 1000, 1),
        total_ms=elapsed_ms(),
    )


def client_for(provider: Provider, timeout: float, max_connections: int) -> httpx.AsyncClient:
    """The HTTP client every provider is measured through: keep-alive, no redirects."""
    return httpx.AsyncClient(
        transport=provider.transport(),
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(
            max_connections=max_connections, max_keepalive_connections=max_connections
        ),
    )


def build_record(
    ctx: RunContext,
    task: Task,
    provider: Provider,
    step: Step1,
    *,
    raw: RawResponse | None = None,
    scheduled_at: str | None = None,
    started_at: str | None = None,
    dispatch_lag_ms: float | None = None,
    warmup: bool = False,
    control: bool = False,
) -> Record:
    billed = None
    if raw is not None and raw.error is None:
        try:
            billed = provider.billed_units(raw)
        except Exception:  # noqa: BLE001 - not knowing the charge must not lose the record
            billed = None
    return Record(
        run_id=ctx.run_id,
        config_hash=ctx.config_hash,
        methodology_version=ctx.methodology_version,
        pool=task.pool,
        headline=task.headline,
        task_id=task.task_id,
        query_id=task.query_id,
        query=task.query,
        country=task.country,
        locale=task.locale,
        device=task.device,
        page=task.page,
        location=task.location,
        run_index=ctx.run_index,
        provider=provider.id,
        provider_status=provider.config.status,
        adapter_version=provider.config.adapter.version,
        scheduled_at=scheduled_at,
        started_at=started_at,
        dispatch_lag_ms=dispatch_lag_ms,
        client_lag=dispatch_lag_ms is not None and dispatch_lag_ms > CLIENT_LAG_THRESHOLD_MS,
        ttfb_ms=raw.ttfb_ms if raw else None,
        total_ms=raw.total_ms if raw else None,
        http_status=raw.status if raw else None,
        bytes=len(raw.body) if raw and raw.error is None else None,
        outcome=str(step.outcome),
        void_reason=step.void_reason,
        checks=step.checks,
        organic_count=step.organic_count,
        results=step.results,
        provider_request_id=step.serp.provider_request_id if step.serp else None,
        substituted=step.serp.substituted if step.serp else None,
        billed_units=billed,
        warmup=warmup,
        control=control,
    )


def _unsupported_detail(provider: Provider, task: Task) -> str:
    needs = []
    if task.device is Device.mobile:
        needs.append("mobile results")
    if task.page > 1:
        needs.append("results pages after the first")
    if task.location:
        needs.append("a place below country level")
    return f"the provider does not offer {' or '.join(needs) or 'this kind of request'}"


async def run_tasks(
    *,
    tasks: Sequence[Task],
    providers: Sequence[Provider],
    writer: RecordWriter,
    ctx: RunContext,
    timeout: float,
    interval: float,
    max_in_flight: int,
    max_requests_per_provider: int,
    save_bodies: Path | None = None,
    on_record: OnRecord | None = None,
    max_control_repeats: int = 0,
) -> list[Record]:
    """Run `tasks` against `providers`. Returns the records, which are also written as they
    are produced (in order of completion, not of dispatch).

    The budget guard is here: a provider is never sent more than `max_requests_per_provider`
    requests in a run, the warm-up and the control repeats included. Tasks beyond that are
    dropped for everyone, so all providers still receive exactly the same tasks.

    `max_control_repeats` is how many control repeats one provider may get after the
    timetable; 0 switches them off.
    """
    tasks = list(tasks)[: max(0, max_requests_per_provider - 1)]
    records: list[Record] = []
    if save_bodies is not None:
        save_bodies.mkdir(parents=True, exist_ok=True)
    clients = {p.id: client_for(p, timeout, max_in_flight) for p in providers}
    in_flight = dict.fromkeys(clients, 0)

    def keep(record: Record) -> Record:
        records.append(record)
        writer.write(record)
        writer.flush()
        if on_record is not None:
            on_record(record)
        return record

    async def measure(
        provider: Provider,
        task: Task,
        *,
        due: float | None = None,
        scheduled_at: str | None = None,
        warmup: bool = False,
        control: bool = False,
    ) -> None:
        try:
            request = provider.build_request(task)
            started = time.perf_counter()
            started_at = _iso(dt.datetime.now(dt.UTC))
            lag = None if due is None else round((started - due) * 1000, 1)
            raw = await send(clients[provider.id], request, timeout)
            if save_bodies is not None and raw.body:
                name = f"{task.task_id}.{provider.id}{'.control' if control else ''}.body"
                (save_bodies / name).write_bytes(raw.body)
            step = judge(provider, task, raw, timeout=timeout, dispatch_lag_ms=lag)
            keep(
                build_record(
                    ctx,
                    task,
                    provider,
                    step,
                    raw=raw,
                    scheduled_at=scheduled_at,
                    started_at=started_at,
                    dispatch_lag_ms=lag,
                    warmup=warmup,
                    control=control,
                )
            )
        finally:
            if not (warmup or control):
                in_flight[provider.id] -= 1

    pending: dict[asyncio.Task[None], tuple[Provider, Task, str]] = {}
    try:
        key, text, country, locale = WARMUP_QUERY
        warmup_task = Task(
            task_id=f"{ctx.run_id}-warmup",
            pool="warmup",
            headline=False,
            query_id=key,
            query=text,
            country=country,
            locale=locale,
        )
        await asyncio.gather(*(measure(p, warmup_task, warmup=True) for p in providers))

        t0 = time.perf_counter()
        wall0 = dt.datetime.now(dt.UTC)
        for index, task in enumerate(tasks):
            due = t0 + index * interval
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            scheduled_at = _iso(wall0 + dt.timedelta(seconds=index * interval))
            for provider in providers:
                if not provider.supports(task):
                    step = not_sent(VOID_NOT_SUPPORTED, _unsupported_detail(provider, task))
                    keep(build_record(ctx, task, provider, step, scheduled_at=scheduled_at))
                elif in_flight[provider.id] >= max_in_flight:
                    detail = (
                        f"{in_flight[provider.id]} of our requests to this provider were still "
                        f"in flight (limit {max_in_flight}); not sent"
                    )
                    step = not_sent(VOID_CLIENT_BACKPRESSURE, detail)
                    keep(build_record(ctx, task, provider, step, scheduled_at=scheduled_at))
                else:
                    in_flight[provider.id] += 1
                    job = asyncio.create_task(
                        measure(provider, task, due=due, scheduled_at=scheduled_at)
                    )
                    pending[job] = (provider, task, scheduled_at)
            for job in [job for job in pending if job.done()]:
                del pending[job]
                job.result()  # an adapter bug stops the run instead of hiding
        await asyncio.gather(*pending)

        if max_control_repeats > 0:
            by_id = {task.task_id: task for task in tasks}
            sent = Counter(record.provider for record in records if record.started_at)
            repeats: dict[str, list[Task]] = {}
            for record in control_candidates(records):
                room = min(max_control_repeats, max_requests_per_provider - sent[record.provider])
                chosen = repeats.setdefault(record.provider, [])
                if len(chosen) < room:
                    chosen.append(by_id[record.task_id])

            async def repeat(provider: Provider) -> None:
                for task in repeats.get(provider.id, []):
                    await measure(provider, task, control=True)

            await asyncio.gather(*(repeat(provider) for provider in providers))
    except asyncio.CancelledError:
        for job, (provider, task, scheduled_at) in pending.items():
            if not job.done():
                job.cancel()
                step = not_sent(VOID_RUN_ABORTED, "the run was aborted before an answer arrived")
                keep(build_record(ctx, task, provider, step, scheduled_at=scheduled_at))
        raise
    finally:
        for job in pending:
            job.cancel()
        for client in clients.values():
            await client.aclose()
    return records
