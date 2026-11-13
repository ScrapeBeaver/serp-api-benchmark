"""Provider fixtures: recorded (or documented) responses an adapter must read correctly.

A fixture is a JSON file in `providers/<id>/fixtures/`:

    {
      "description": "what this response is",
      "source": "recorded on <date>" or "built from the documented shape (<url>)",
      "task": {"query": "...", "country": "GB", "locale": "en", "device": "desktop", "page": 1},
      "response": {"status": 200, "headers": {...}, "body": {...}},    (or "text": "raw body")
      "expect": {"outcome": "delivered", "organic_count": 10, "billed_units": 1.0}
    }

`expect.outcome` is required; `void_reason`, `organic_count`, `billed_units` and
`provider_request_id` are checked when present. The same check runs in the test suite and
in `serp-bench check-providers`, so a provider cannot pass one and fail the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serp_bench.models import OUTCOME_DELIVERED, VALID_OUTCOMES, RawResponse, Task
from serp_bench.provider import Provider, ProviderSpec
from serp_bench.validate import judge

DUMMY_SECRET = "serp-bench-dummy-secret-0123456789"


@dataclass(frozen=True)
class Fixture:
    path: Path
    description: str
    source: str
    task: Task
    raw: RawResponse
    expect: dict[str, Any]


def dummy_env(spec: ProviderSpec) -> dict[str, str]:
    """An environment with a recognisable placeholder for every secret the provider needs."""
    return {name: f"{DUMMY_SECRET}-{name}" for name in spec.config.auth.env}


def load_fixture(path: Path) -> Fixture:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("description", "source", "task", "response", "expect"):
        if key not in data:
            raise ValueError(f"{path.name}: missing `{key}`")
    response = data["response"]
    if "text" in response:
        body = str(response["text"]).encode("utf-8")
    else:
        body = json.dumps(response.get("body"), ensure_ascii=False).encode("utf-8")
    outcome = data["expect"].get("outcome")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"{path.name}: expect.outcome '{outcome}' is not a known outcome")
    task = Task(
        task_id=f"fixture-{path.stem}",
        pool="fixture",
        headline=False,
        query_id=path.stem,
        **data["task"],
    )
    raw = RawResponse(
        status=int(response["status"]),
        headers={str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()},
        body=body,
        ttfb_ms=1.0,
        total_ms=1.0,
    )
    return Fixture(path, data["description"], data["source"], task, raw, data["expect"])


def fixtures_of(spec: ProviderSpec) -> list[Path]:
    return sorted(spec.fixtures_dir.glob("*.json")) if spec.fixtures_dir.is_dir() else []


def check_fixture(provider: Provider, fixture: Fixture) -> list[str]:
    """Problems found reading one fixture; empty when the adapter reads it as expected."""
    problems = []
    step = judge(provider, fixture.task, fixture.raw)
    expect = fixture.expect
    if step.outcome != expect["outcome"]:
        trail = step.checks[-1]
        problems.append(
            f"outcome is '{step.outcome}', expected '{expect['outcome']}' "
            f"(last check: {trail.check}: {trail.detail})"
        )
        return problems
    if "void_reason" in expect and step.void_reason != expect["void_reason"]:
        problems.append(f"void_reason is '{step.void_reason}', expected '{expect['void_reason']}'")
    if "organic_count" in expect and step.organic_count != expect["organic_count"]:
        problems.append(
            f"organic_count is {step.organic_count}, expected {expect['organic_count']}"
        )
    if "provider_request_id" in expect:
        found = step.serp.provider_request_id if step.serp else None
        if found != expect["provider_request_id"]:
            problems.append(f"provider_request_id is {found!r}")
    if "billed_units" in expect:
        try:
            billed = provider.billed_units(fixture.raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            billed = None
        if billed != expect["billed_units"]:
            problems.append(f"billed_units is {billed}, expected {expect['billed_units']}")
    if step.outcome == OUTCOME_DELIVERED and step.serp is not None:
        positions = [result.position for result in step.serp.organic]
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            problems.append(f"positions are not ascending and unique: {positions}")
    return problems
