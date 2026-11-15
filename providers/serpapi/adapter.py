"""SerpApi: GET /search with `engine=google` (Google Search API), parsed JSON.

Documentation: https://serpapi.com/search-api
Status and error codes: https://serpapi.com/api-status-and-error-codes
"""

from __future__ import annotations

from serp_bench.ai_overview import read as read_ai_overview
from serp_bench.models import (
    VOID_AUTH_OR_CREDITS,
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

API_KEY = "SERPAPI_API_KEY"

# `search_information.organic_results_state`, Google's note about the result set.
_EXACT = "results for exact spelling"
_SUBSTITUTED = "empty showing"  # nothing for the query as asked; results for something close

# The `error` text of a search that worked and found nothing.
_NO_RESULTS = "hasn't returned any results"
# HTTP 429 covers two things: the hourly limit of the plan, and a plan with no searches
# left. Only the second is told apart here, by its text.
_OUT_OF_SEARCHES = "run out of searches"


class SerpApi(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        params = {
            # The key travels in the address. The runner logs requests through `loggable`,
            # which removes it.
            "api_key": self.secret(API_KEY),
            "engine": "google",
            "q": task.query,
            "gl": task.country.lower(),
            "hl": task.locale.split("-")[0].lower(),
            # An identical search is answered from a cache for an hour, free of charge.
            # The benchmark measures a search, so the cache is switched off.
            "no_cache": "true",
        }
        if task.device is Device.mobile:
            params["device"] = "mobile"
        if task.page > 1:
            params["start"] = str((task.page - 1) * 10)  # an offset: 10 is the second page
        if task.location:
            params["location"] = task.location  # the canonical name, at city level
        return HttpRequest(method="GET", url=f"{self.base_url}/search", params=params)

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
            provider_request_id=(data.get("search_metadata") or {}).get("id"),
            echo=Echo(query=asked.get("q"), country=asked.get("gl"), locale=asked.get("hl")),
            substituted=True if _SUBSTITUTED in state else False if state == _EXACT else None,
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """A 200 can carry an `error`: a search that worked and found nothing (status
        "Success" with an error text), or one that failed on their side (status "Error")."""
        data = raw.json()
        error = str(data.get("error") or "")
        if (data.get("search_metadata") or {}).get("status") == "Error":
            return FailureClass.provider_error
        if not error:
            return None
        return FailureClass.empty_results if _NO_RESULTS in error else FailureClass.provider_error

    def own_fault(self, raw: RawResponse) -> str | None:
        """A plan with no searches left answers 429, the status of a rate limit."""
        if raw.status != 429:
            return None
        text = raw.body.decode("utf-8", errors="replace")
        return VOID_AUTH_OR_CREDITS if _OUT_OF_SEARCHES in text else None
