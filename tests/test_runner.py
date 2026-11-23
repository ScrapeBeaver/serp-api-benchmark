"""The run protocol: an open-loop timetable, the same for every provider."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import FakeProvider, echo_body, fake_config, make_tasks

from serp_bench.models import SCHEMA_VERSION, Device, Record
from serp_bench.poison import ONE_OFF, POISONED, judge
from serp_bench.records import RecordWriter, read_records
from serp_bench.report import summarise
from serp_bench.runner import RunContext, run_tasks

CTX = RunContext("test-run", "cfg000000000", "test", 0)


def run(tmp_path, providers, tasks, **options) -> list[Record]:
    settings = {
        "timeout": 5.0,
        "interval": 0.01,
        "max_in_flight": 4,
        "max_requests_per_provider": 1000,
    }
    settings.update(options)
    with RecordWriter(tmp_path / "records.jsonl.gz") as writer:
        return asyncio.run(
            run_tasks(tasks=tasks, providers=providers, writer=writer, ctx=CTX, **settings)
        )


def real(records, provider=None):
    return [r for r in records if not r.warmup and (provider is None or r.provider == provider)]


def slow(seconds: float):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        return httpx.Response(200, json=echo_body(request))

    return handler


def test_one_record_per_task_and_provider_plus_a_warmup(tmp_path, specs):
    providers = [FakeProvider(fake_config(specs, "a")), FakeProvider(fake_config(specs, "b"))]
    records = run(tmp_path, providers, make_tasks(6))
    assert len(records) == (6 + 1) * 2
    warmups = [r for r in records if r.warmup]
    assert {r.provider for r in warmups} == {"a", "b"} and all(r.pool == "warmup" for r in warmups)
    for provider in ("a", "b"):
        assert sorted(r.task_id for r in real(records, provider)) == [
            f"t-{i:03d}" for i in range(1, 7)
        ]
    assert all(r.outcome == "delivered" for r in real(records))


def test_every_provider_gets_a_task_at_the_same_scheduled_instant(tmp_path, specs):
    providers = [FakeProvider(fake_config(specs, "a")), FakeProvider(fake_config(specs, "b"))]
    records = real(run(tmp_path, providers, make_tasks(5)))
    by_task: dict[str, set] = {}
    for record in records:
        by_task.setdefault(record.task_id, set()).add(record.scheduled_at)
    assert all(len(times) == 1 for times in by_task.values())
    assert len({next(iter(times)) for times in by_task.values()}) == 5


def test_a_slow_provider_does_not_slow_the_timetable(tmp_path, specs):
    """The open loop: a provider that takes 10 intervals to answer still receives every
    task on schedule, and its neighbour is not held back by it."""
    interval, tasks = 0.02, make_tasks(8)
    providers = [
        FakeProvider(fake_config(specs, "fast")),
        FakeProvider(fake_config(specs, "slow"), slow(10 * interval)),
    ]
    started = time.perf_counter()
    records = real(run(tmp_path, providers, tasks, interval=interval, max_in_flight=20))
    elapsed = time.perf_counter() - started

    closed_loop_would_take = len(tasks) * 10 * interval
    assert elapsed < closed_loop_would_take * 0.6
    for name in ("fast", "slow"):
        mine = [r for r in records if r.provider == name]
        assert len(mine) == 8 and all(r.outcome == "delivered" for r in mine)
        assert all(r.dispatch_lag_ms is not None and r.dispatch_lag_ms < 150 for r in mine)
    slow_latencies = [r.total_ms for r in records if r.provider == "slow"]
    assert min(slow_latencies) >= 10 * interval * 1000 * 0.9, "latency runs from the actual send"


def test_exceeding_the_in_flight_cap_is_our_fault_and_only_that_providers(tmp_path, specs):
    providers = [
        FakeProvider(fake_config(specs, "fast")),
        FakeProvider(fake_config(specs, "slow"), slow(0.2)),
    ]
    records = real(run(tmp_path, providers, make_tasks(8), interval=0.01, max_in_flight=2))
    slow_records = [r for r in records if r.provider == "slow"]
    voided = [r for r in slow_records if r.outcome == "void"]
    assert len(slow_records) == 8 and len(voided) == 6
    assert {r.void_reason for r in voided} == {"client_backpressure"}
    assert all(r.started_at is None and r.total_ms is None for r in voided)
    assert voided[0].checks[0].check == "dispatched" and "in flight" in voided[0].checks[0].detail
    assert all(r.outcome == "delivered" for r in records if r.provider == "fast")


def test_a_task_the_provider_does_not_offer_is_void_not_failed(tmp_path, specs):
    providers = [
        FakeProvider(fake_config(specs, "full")),
        FakeProvider(fake_config(specs, "desktop_only", mobile=False)),
    ]
    records = real(run(tmp_path, providers, make_tasks(3, device=Device.mobile)))
    limited = [r for r in records if r.provider == "desktop_only"]
    assert [(r.outcome, r.void_reason) for r in limited] == [("void", "not_supported")] * 3
    assert "mobile results" in limited[0].checks[0].detail
    assert all(r.outcome == "delivered" for r in records if r.provider == "full")


def test_the_hard_time_limit(tmp_path, specs):
    provider = FakeProvider(fake_config(specs), slow(1.0))
    record = real(run(tmp_path, [provider], make_tasks(1), timeout=0.1))[0]
    assert record.outcome == "timeout"
    assert 80 <= record.total_ms <= 400
    assert record.http_status is None and record.bytes is None


def test_a_network_error_is_a_failure_of_its_own_class(tmp_path, specs):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    record = real(run(tmp_path, [FakeProvider(fake_config(specs), refuse)], make_tasks(1)))[0]
    assert record.outcome == "network"
    assert "ConnectError" in record.checks[-1].detail


def test_our_own_auth_failure_is_void(tmp_path, specs):
    provider = FakeProvider(fake_config(specs), lambda request: httpx.Response(401, json={}))
    records = real(run(tmp_path, [provider], make_tasks(3)))
    assert [(r.outcome, r.void_reason) for r in records] == [("void", "auth_or_credits")] * 3


def test_the_budget_guard_counts_the_warmup(tmp_path, specs):
    providers = [FakeProvider(fake_config(specs, "a")), FakeProvider(fake_config(specs, "b"))]
    records = run(tmp_path, providers, make_tasks(10), max_requests_per_provider=4)
    for name in ("a", "b"):
        assert len([r for r in records if r.provider == name]) == 4  # 1 warm-up + 3 tasks
    assert {r.task_id for r in real(records)} == {"t-001", "t-002", "t-003"}


def test_redirects_are_not_followed(tmp_path, specs):
    calls = []

    def redirect(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.invalid/"})

    record = real(run(tmp_path, [FakeProvider(fake_config(specs), redirect)], make_tasks(1)))[0]
    assert record.outcome == "provider_error" and record.http_status == 302
    assert all("elsewhere" not in url for url in calls)


def test_no_retry_is_made(tmp_path, specs):
    calls = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["task_id"])
        return httpx.Response(503, json={})

    run(tmp_path, [FakeProvider(fake_config(specs), fail)], make_tasks(4))
    assert sorted(c for c in calls if c.startswith("t-")) == ["t-001", "t-002", "t-003", "t-004"]


def test_records_keep_where_results_point_and_not_what_they_say(tmp_path, specs):
    records = run(tmp_path, [FakeProvider(fake_config(specs))], make_tasks(2))
    record = real(records)[0]
    data = record.model_dump()
    assert data["schema_version"] == SCHEMA_VERSION
    assert set(data["results"][0]) == {
        "pos",
        "host",
        "url_hash",
        "on_google",
        "wrapper",
        "well_formed",
    }
    text = record.model_dump_json()
    assert "Result 1" not in text, "no titles in a record"
    assert "page-1" not in text, "no URLs in a record, only their host and hash"
    assert record.provider_status == "shadow" and record.run_index == 0
    assert record.scheduled_at and record.started_at and record.dispatch_lag_ms is not None
    assert [c.check for c in record.checks][0] == "dispatched"


def test_the_file_holds_exactly_what_was_returned(tmp_path, specs):
    records = run(tmp_path, [FakeProvider(fake_config(specs))], make_tasks(5))
    stored = list(read_records(tmp_path / "records.jsonl.gz"))
    assert stored == records


def test_bodies_are_saved_only_on_request(tmp_path, specs):
    bodies = tmp_path / "bodies"
    run(tmp_path, [FakeProvider(fake_config(specs))], make_tasks(2), save_bodies=bodies)
    saved = sorted(p.name for p in bodies.iterdir())
    assert "t-001.fake.body" in saved and "t-002.fake.body" in saved


def test_an_adapter_bug_stops_the_run(tmp_path, specs):
    class Broken(FakeProvider):
        def build_request(self, task):
            if task.task_id == "t-002":
                raise RuntimeError("adapter bug")
            return super().build_request(task)

    with pytest.raises(RuntimeError, match="adapter bug"):
        run(tmp_path, [Broken(fake_config(specs))], make_tasks(3))


def test_the_mock_is_deterministic(tmp_path, specs):
    config = specs["mock"].config
    fast = config.adapter.model_copy(
        update={"options": config.adapter.options | {"time_scale": 0.001}}
    )
    spec_config = config.model_copy(update={"adapter": fast})
    outcomes = []
    for attempt in ("one", "two"):
        provider = specs["mock"].adapter_class(spec_config, {})
        folder = tmp_path / attempt
        folder.mkdir()
        # 20 ms between tasks against answers that take a few: a busy machine would have to
        # stall for a tenth of a second before our own requests pile up and one is set aside.
        # With 3 ms this test failed now and then under load, for a reason that was ours.
        records = real(run(folder, [provider], make_tasks(60), interval=0.02))
        outcomes.append({r.task_id: (r.outcome, r.organic_count) for r in records})
    assert outcomes[0] == outcomes[1]
    seen = {outcome for outcome, _ in outcomes[0].values()}
    assert "delivered" in seen and len(seen) > 1, "the mock exercises failures too"


# --- control repeats (step 3) ----------------------------------------------------------------


def odd_page(marker: str = "odd"):
    """A handler that answers with ten sites nobody else returns."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = echo_body(request)
        body["results"] = [
            {"url": f"https://www.{marker}{i}.example/", "title": f"Other {i}"}
            for i in range(1, 11)
        ]
        return httpx.Response(200, json=body)

    return handler


def four(specs, handler):
    """Providers a, b and d answer alike, which makes them a group; c answers through
    `handler`."""
    return [
        FakeProvider(fake_config(specs, "a")),
        FakeProvider(fake_config(specs, "b")),
        FakeProvider(fake_config(specs, "c"), handler),
        FakeProvider(fake_config(specs, "d")),
    ]


def test_no_control_repeats_unless_asked_for(tmp_path, specs):
    records = run(tmp_path, four(specs, odd_page()), make_tasks(3))
    assert not any(r.control for r in records)


def test_a_page_that_matches_nobody_is_asked_for_once_more(tmp_path, specs):
    calls: list[str] = []

    def once_odd(request: httpx.Request) -> httpx.Response:
        """The first answer to task t-002 is a different page; every other answer is real."""
        task_id = json.loads(request.content)["task_id"]
        calls.append(task_id)
        if task_id == "t-002" and calls.count("t-002") == 1:
            return odd_page()(request)
        return httpx.Response(200, json=echo_body(request))

    records = run(tmp_path, four(specs, once_odd), make_tasks(3), max_control_repeats=5)
    controls = [r for r in records if r.control]
    assert [(r.provider, r.task_id, r.outcome) for r in controls] == [("c", "t-002", "delivered")]
    # It is sent after the last measured request, and is not one of them.
    assert records.index(controls[0]) == len(records) - 1
    assert controls[0].scheduled_at is None and not controls[0].counts
    assert len([r for r in records if r.counts]) == 12
    assert calls.count("t-002") == 2 and calls[-1] == "t-002"

    verdict = judge(records)[("test-run", "t-002", "c")]
    assert (verdict.verdict, verdict.control) == (POISONED, ONE_OFF)
    assert {report.provider: report.tasks for report in summarise(records)} == {
        "a": 3,
        "b": 3,
        "c": 3,
        "d": 3,
    }


def test_control_repeats_are_capped_and_stay_inside_the_run_limit(tmp_path, specs):
    capped = run(tmp_path, four(specs, odd_page()), make_tasks(4), max_control_repeats=2)
    assert [r.task_id for r in capped if r.control] == ["t-001", "t-002"]

    # A run limit of 5 requests: the warm-up and four tasks leave no room for a repeat.
    full = run(
        tmp_path,
        four(specs, odd_page()),
        make_tasks(4),
        max_control_repeats=2,
        max_requests_per_provider=5,
    )
    assert not any(r.control for r in full)


def test_second_pages_are_not_repeated(tmp_path, specs):
    records = run(tmp_path, four(specs, odd_page()), make_tasks(2, page=2), max_control_repeats=5)
    assert not any(r.control for r in records)
