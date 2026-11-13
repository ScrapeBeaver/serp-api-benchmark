"""Decodo: POST /v1/search (Web Data API).

Documentation:
https://help.decodo.com/docs/web-data-api-search-introduction
https://help.decodo.com/docs/web-data-api-search-parameters
"""

from __future__ import annotations

from serp_bench.models import (
    Echo,
    FailureClass,
    HttpRequest,
    OrganicResult,
    RawResponse,
    Serp,
    Task,
)
from serp_bench.provider import Provider
from serp_bench.uule import uule

API_KEY = "DECODO_API_KEY"


class Decodo(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        # The endpoint has six parameters: query, geo, uule, gl, locale, page. There is no
        # device parameter, which is why the provider is declared without mobile.
        body: dict[str, object] = {
            "query": task.query,
            "geo": task.country.upper(),  # a 2-letter code; an unknown value falls back to the US
            "locale": task.locale,
        }
        if task.page > 1:
            body["page"] = task.page
        if task.location:
            body["uule"] = uule(task.location)  # "if both geo and uule are set, uule is used"
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/v1/search",
            headers={
                "Authorization": f"Bearer {self.secret(API_KEY)}",
                "Accept": "application/json",
            },
            json_body=body,
        )

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["rank"]),
                url=item["link"],
                title=item["title"],
                snippet=item.get("description"),
            )
            for item in data["organic"]
        ]
        used = data.get("search_parameters") or {}
        return Serp(
            organic=organic,
            echo=Echo(query=used.get("query"), country=used.get("gl"), locale=used.get("locale")),
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """`status` is `done` or `faulted`. A search with no results is `done` with an empty
        `organic`, which the shared checks record as an empty page, not as an error."""
        if raw.json().get("status") == "faulted":
            return FailureClass.provider_error
        return None
