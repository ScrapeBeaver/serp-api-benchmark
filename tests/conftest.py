from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from serp_bench.contract import ProviderConfig
from serp_bench.models import Device, Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider, ProviderSpec, discover, parse_config

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "providers"
CONTRACTS_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"
BENCH = ROOT / "bench.yaml"
# The shipped bench.yaml is in force from its `starts`. A day before that is planned from the
# file it names as `previous`, so a test that means one of the two says which day it is.
DAY = dt.date(2026, 11, 12)  # the configuration before: bench-0.1.yaml
NEW_DAY = dt.date(2026, 11, 26)  # the first day of the shipped bench.yaml


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach a real provider. The `.env` next to bench.yaml holds live keys on a
    developer's machine, and the command line loads it; here that loading is switched off
    and every provider key is removed from the environment, for every test. A test that
    needs a key sets a fake one itself."""
    from serp_bench import cli

    monkeypatch.setattr(cli, "_load_dotenv", lambda path: None)
    for spec in discover(PROVIDERS_DIR).values():
        for name in (*spec.config.auth.env, spec.config.adapter.base_url_env):
            if name:
                monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def specs() -> dict[str, ProviderSpec]:
    return discover(PROVIDERS_DIR)


@pytest.fixture(scope="session")
def credits() -> ProviderConfig:
    return parse_config(CONTRACTS_DIR / "credit_subscription.yaml")


@pytest.fixture(scope="session")
def rates() -> ProviderConfig:
    return parse_config(CONTRACTS_DIR / "rate_card.yaml")


@pytest.fixture(scope="session")
def payg() -> ProviderConfig:
    return parse_config(CONTRACTS_DIR / "pay_as_you_go.yaml")


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "task_id": "t-001",
        "pool": "core",
        "headline": True,
        "query_id": "q-001",
        "query": "coffee shops london",
        "country": "GB",
        "locale": "en",
        "device": Device.desktop,
        "page": 1,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def make_tasks(count: int, **overrides: object) -> list[Task]:
    return [
        make_task(task_id=f"t-{i:03d}", query_id=f"q-{i:03d}", query=f"query {i}", **overrides)
        for i in range(1, count + 1)
    ]


def ok_body(query: str = "coffee shops london", country: str = "GB", count: int = 10) -> dict:
    return {
        "q": query,
        "gl": country,
        "results": [
            {"url": f"https://www.site{i}.example/page-{i}", "title": f"Result {i}"}
            for i in range(1, count + 1)
        ],
    }


def raw(
    status: int = 200, body: Any = None, *, text: str | None = None, **more: Any
) -> RawResponse:
    payload = text.encode() if text is not None else json.dumps(body).encode()
    return RawResponse(status=status, body=payload, ttfb_ms=10.0, total_ms=20.0, **more)


Handler = Callable[[httpx.Request], Any]


class FakeProvider(Provider):
    """A provider whose answers a test writes itself."""

    def __init__(self, config: ProviderConfig, handler: Handler | None = None) -> None:
        super().__init__(config, {name: f"secret-{name}" for name in config.auth.env})
        self._handler = handler or (lambda request: httpx.Response(200, json=echo_body(request)))

    def build_request(self, task: Task) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url="https://fake.invalid/search",
            json_body={"q": task.query, "gl": task.country, "task_id": task.task_id},
        )

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self._handler)

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        return Serp(
            organic=[
                OrganicResult(position=i, url=item["url"], title=item["title"])
                for i, item in enumerate(data["results"], start=1)
            ],
            provider_request_id=data.get("id"),
            echo=Echo(query=data.get("q"), country=data.get("gl")),
        )


def echo_body(request: httpx.Request) -> dict:
    asked = json.loads(request.content)
    return ok_body(asked["q"], asked["gl"])


def fake_config(
    specs: dict[str, ProviderSpec], provider_id: str = "fake", **changes: Any
) -> ProviderConfig:
    """The mock's configuration under another id, with capabilities or status changed."""
    base = specs["mock"].config
    update: dict[str, Any] = {"id": provider_id, "name": provider_id.title()}
    capabilities = {k: v for k, v in changes.items() if k in type(base.capabilities).model_fields}
    if capabilities:
        update["capabilities"] = base.capabilities.model_copy(update=capabilities)
    if "status" in changes:
        update["status"] = changes["status"]
    return base.model_copy(update=update)
