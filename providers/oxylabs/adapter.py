"""Oxylabs: POST /v1/scrape/google/search (Web API, dedicated Google scraper), parsed.

Documentation:
https://developers.oxylabs.io/api-reference/dedicated-scrapers/search-engine-scrapers
https://developers.oxylabs.io/products/web-api/scrape
https://developers.oxylabs.io/products/web-api/authentication
"""

from __future__ import annotations

from serp_bench.models import (
    Device,
    Echo,
    FailureClass,
    HttpRequest,
    OrganicResult,
    RawResponse,
    Serp,
    Task,
)
from serp_bench.provider import Provider

API_KEY = "OXYLABS_API_KEY"


class Oxylabs(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        language = task.locale.split("-")[0].lower()
        body: dict[str, object] = {
            "query": task.query,
            "location": task.country.upper(),  # ISO 3166-1 alpha-2; lower case is refused
            "locale": f"{language}_{task.country.upper()}",  # interface language, e.g. en_US
            "output": ["json"],  # the built-in parser; the default is raw HTML
        }
        if task.device is Device.mobile:
            body["device"] = "mobile"
        if task.page > 1:
            body["start_page"] = task.page
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/v1/scrape/google/search",
            headers={"Authorization": f"Bearer {self.secret(API_KEY)}"},
            json_body=body,
        )

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        page = data["results"][0]["json"]
        found = page["results"].get("search_information") or {}
        organic = [
            OrganicResult(
                position=int(item["pos"]),
                url=item["url"],
                title=item["title"],
                snippet=item.get("desc"),
                displayed_url=item.get("url_shown"),
            )
            for item in page["results"]["organic"]
        ]
        asked = data.get("params") or {}
        return Serp(
            organic=organic,
            provider_request_id=(data.get("metadata") or {}).get("request_id"),
            # Google found nothing for the query as asked and shows results for another one.
            substituted=found.get("no_results_for_original_query_found"),
            echo=Echo(query=asked.get("query"), country=asked.get("location")),
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """The answer says how it went: `state` is `done`, and each result carries the
        status of the fetch (`HTTP_OK`) and of the parser (`PARSE_SUCCESS`, code 12000).
        Anything else in a 2xx answer is a failure the service reports itself."""
        data = raw.json()
        if data.get("state") != "done":
            return FailureClass.provider_error
        for result in data.get("results") or []:
            statuses = (result.get("metadata") or {}).get("statuses") or {}
            fetched = (statuses.get("http_request") or {}).get("code")
            parsed = (statuses.get("json_parse") or {}).get("code")
            if (fetched is not None and not 200 <= fetched < 300) or parsed not in (None, 12000):
                return FailureClass.provider_error
        return None
