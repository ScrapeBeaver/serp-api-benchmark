"""Serper: POST /search.

The API documentation is behind a login (https://serper.dev/). The request and the
response below are the ones its dashboard shows; what a live call returned is recorded in
`fixtures/`.
"""

from __future__ import annotations

from serp_bench.models import Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider

API_KEY = "SERPER_API_KEY"


class Serper(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        body: dict[str, object] = {
            "q": task.query,
            "gl": task.country.lower(),
            "hl": task.locale.split("-")[0].lower(),
        }
        if task.page > 1:
            body["page"] = task.page
        if task.location:
            body["location"] = task.location  # the place by name, as Google spells it
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/search",
            headers={"X-API-KEY": self.secret(API_KEY)},
            json_body=body,
        )

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["position"]),
                url=item["link"],
                title=item["title"],
                snippet=item.get("snippet"),
            )
            for item in data["organic"]
        ]
        used = data.get("searchParameters") or {}
        return Serp(
            organic=organic,
            echo=Echo(query=used.get("q"), country=used.get("gl"), locale=used.get("hl")),
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        """The `credits` field of the answer, when it is there."""
        credits = raw.json().get("credits")
        return None if credits is None else float(credits)
