"""Scrapingdog: GET /google (Google Search API, standard search).

Documentation: https://www.scrapingdog.com/documentation/google-search-api/
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from serp_bench.ai_overview import read as read_ai_overview
from serp_bench.models import Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider
from serp_bench.uule import uule

API_KEY = "SCRAPINGDOG_API_KEY"

# `search_information.organic_results_state`, Google's note about the result set.
_EXACT = "results for exact spelling"
_SUBSTITUTED = "empty showing"  # nothing for the query as asked; results for something close


class Scrapingdog(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        params = {
            # The key travels in the address: the API documents no header for it. The runner
            # logs requests through `loggable`, which removes it.
            "api_key": self.secret(API_KEY),
            "query": task.query,
            "country": task.country.lower(),
            "language": task.locale.split("-")[0].lower(),  # a language, not a locale: "en", "de"
            "page": str(task.page - 1),  # the first page is 0
            "advance_search": "false",  # the standard search, 5 credits
        }
        if task.device.value == "mobile":
            params["mob_search"] = "true"  # documented at 10 credits
        if task.location:
            params["uule"] = uule(task.location)
        if task.time_range == "week":
            params["tbs"] = "qdr:w"
        return HttpRequest(method="GET", url=f"{self.base_url}/google", params=params)

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["rank"]),
                url=item["link"],
                title=item["title"],
                snippet=item.get("snippet"),
                displayed_url=item.get("displayed_link"),
            )
            for item in data["organic_results"]
        ]
        info = data.get("search_information") or {}
        # `query_displayed` is the query after Google's spelling correction, so it is not
        # what was asked. The Google address the response reports is.
        used = parse_qs(urlparse(info.get("url") or "").query)
        state = str(info.get("organic_results_state") or "").lower()
        return Serp(
            organic=organic,
            ai_overview=read_ai_overview(data.get("ai_overview")),
            echo=Echo(
                query=(used.get("q") or [None])[0],
                country=(used.get("gl") or [None])[0],
                locale=(used.get("hl") or [None])[0],
            ),
            substituted=True if _SUBSTITUTED in state else False if state == _EXACT else None,
        )
