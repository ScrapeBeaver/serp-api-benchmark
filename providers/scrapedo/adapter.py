"""Scrape.do: GET /plugin/google/search (ready-made Google Search API), parsed JSON.

Documentation: https://scrape.do/documentation/google-scraper-api/search/search/
"""

from __future__ import annotations

from serp_bench.ai_overview import read as read_ai_overview
from serp_bench.models import (
    Device,
    Echo,
    HttpRequest,
    OrganicResult,
    RawResponse,
    Serp,
    Task,
)
from serp_bench.provider import Provider

TOKEN = "SCRAPEDO_TOKEN"
COST_HEADER = "scrape.do-request-cost"
REQUEST_ID_HEADER = "scrape.do-request-id"

# `search_information.organic_results_state`, Google's note about the result set.
_EXACT = "results for exact spelling"
_SUBSTITUTED = "empty showing"  # nothing for the query as asked; results for something close


def _header(raw: RawResponse, name: str) -> str | None:
    for key, value in raw.headers.items():
        if key.lower() == name:
            return value
    return None


class ScrapeDo(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        params = {
            # The token travels in the address: the API documents no header for it. The
            # runner logs requests through `loggable`, which removes it.
            "token": self.secret(TOKEN),
            "q": task.query,
            "gl": task.country.lower(),
            "hl": task.locale.split("-")[0].lower(),
            # The cache is on by default and would answer a repeated search from memory for
            # six hours. The benchmark measures a search, so it is switched off.
            "cache": "false",
        }
        if task.device is Device.mobile:
            params["device"] = "mobile"
        if task.page > 1:
            params["start"] = str((task.page - 1) * 10)  # an offset in steps of 10
        if task.location:
            params["location"] = task.location  # the canonical name; encoded on their side
        return HttpRequest(method="GET", url=f"{self.base_url}/plugin/google/search", params=params)

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["position"]),
                url=item["link"],
                title=item["title"],
                snippet=item.get("snippet"),
                displayed_url=item.get("displayed_link"),
            )
            for item in data["organic_results"]
        ]
        asked = data.get("search_parameters") or {}
        state = str(
            (data.get("search_information") or {}).get("organic_results_state") or ""
        ).lower()
        return Serp(
            organic=organic,
            ai_overview=read_ai_overview(data.get("ai_overview")),
            provider_request_id=_header(raw, REQUEST_ID_HEADER),
            echo=Echo(query=asked.get("q"), country=asked.get("gl"), locale=asked.get("hl")),
            substituted=True if _SUBSTITUTED in state else False if state == _EXACT else None,
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        """Credits charged, from the `Scrape.do-Request-Cost` header, when it is there."""
        value = _header(raw, COST_HEADER)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None
