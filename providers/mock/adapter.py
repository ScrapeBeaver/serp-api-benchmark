"""An offline provider: deterministic answers, no network.

It goes through the same path as a real provider. The only difference is the transport:
instead of opening a connection, the request is answered in-process by `_answer`. The same
task always gets the same answer, so runs against the mock are reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random

import httpx

from serp_bench.models import Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider

_HOSTS = [
    "atlas", "birch", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
    "kestrel", "lumen", "meadow", "nimbus", "orchard", "pebble", "quartz", "river", "summit",
    "thistle", "umber", "valley", "willow", "yarrow", "zephyr",
]  # fmt: skip


class Mock(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/search",
            json_body={
                "q": task.query,
                "country": task.country,
                "locale": task.locale,
                "device": task.device.value,
                "page": task.page,
            },
        )

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self._answer)

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)
        seed = f"{self.options.get('seed', 0)}|{json.dumps(asked, sort_keys=True)}"
        rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))

        low, high = self.options.get("latency_ms", [200, 1800])
        scale = float(self.options.get("time_scale", 0.1))
        await asyncio.sleep(rng.uniform(low, high) / 1000 * scale)

        roll, outcome = rng.random(), "ok"
        for name, share in self.options.get("failure_shares", {}).items():
            if roll < share:
                outcome = name
                break
            roll -= share

        if outcome == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if outcome == "http_5xx":
            return httpx.Response(503, json={"error": "simulated outage"})
        if outcome == "http_429":
            return httpx.Response(429, json={"error": "simulated rate limit"})
        if outcome == "invalid_json":
            return httpx.Response(200, text="<html><body>Bad gateway</body></html>")

        # A query that is one quoted phrase has no results here, as a nonsense phrase has
        # none on a real search engine. This is what the conformance probe expects.
        phrase = asked["q"].startswith('"') and asked["q"].endswith('"')
        count = 0 if outcome == "empty_results" or phrase else rng.randint(8, 10)
        hosts = rng.sample(_HOSTS, count)
        offset = (asked["page"] - 1) * 10
        results = [
            {
                "rank": offset + index + 1,
                "link": f"https://www.{host}.example/{asked['q'].replace(' ', '-')[:40]}",
                "title": f"{host.title()}: {asked['q']}",
                "text": f"A synthetic result about {asked['q']}.",
                "shown": f"{host}.example",
            }
            for index, host in enumerate(hosts)
        ]
        body = {
            "id": hashlib.sha256(seed.encode()).hexdigest()[:12],
            "query": asked["q"],
            "country": asked["country"],
            "results": results,
        }
        return httpx.Response(200, json=body)

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        return Serp(
            organic=[
                OrganicResult(
                    position=item["rank"],
                    url=item["link"],
                    title=item["title"],
                    snippet=item.get("text"),
                    displayed_url=item.get("shown"),
                )
                for item in data["results"]
            ],
            provider_request_id=data.get("id"),
            echo=Echo(query=data.get("query"), country=data.get("country")),
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        return 1.0 if raw.status == 200 else 0.0
