"""ScrapeBeaver: POST /v1/search/light.

Documentation: https://scrapebeaver.com/docs/google-search/request-and-response
"""

from __future__ import annotations

from serp_bench.ai_overview import read as read_ai_overview
from serp_bench.models import (
    VOID_AUTH_OR_CREDITS,
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

API_KEY = "SCRAPEBEAVER_API_KEY"


class ScrapeBeaver(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        body: dict[str, object] = {
            "query": task.query,
            "country": task.country.upper(),
            "locale": task.locale,
            "device": task.device.value,
        }
        if task.page > 1:
            body["start"] = (task.page - 1) * 10  # 0 is page 1, 10 is page 2
        if task.location:
            body["uule"] = uule(task.location)
        if task.time_range == "week":
            body["time_range"] = "week"
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/v1/search/light",
            headers={"X-API-Key": self.secret(API_KEY)},
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
                displayed_url=item.get("displayed_link"),
            )
            for item in data["organic_results"]
        ]
        used = data.get("search_parameters") or {}
        return Serp(
            organic=organic,
            ai_overview=read_ai_overview(data.get("ai_overview")),
            provider_request_id=(data.get("search_metadata") or {}).get("id"),
            echo=Echo(query=used.get("q"), country=used.get("gl"), locale=used.get("hl")),
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """A failed search keeps the full envelope and says so in three places."""
        data = raw.json()
        status = (data.get("search_metadata") or {}).get("status")
        if data.get("error") or status == "Failed" or data.get("organic_success") is False:
            return FailureClass.provider_error
        return None

    def own_fault(self, raw: RawResponse) -> str | None:
        """429 with `limit: monthly` means the account's monthly searches are used up.
        That is our account, not the service; the other 429s (concurrency, rate) stay
        failures, because a run never exceeds the documented limits."""
        if raw.status != 429:
            return None
        try:
            limit = raw.json().get("limit")
        except (ValueError, AttributeError):
            return None
        return VOID_AUTH_OR_CREDITS if limit == "monthly" else None

    def billed_units(self, raw: RawResponse) -> float | None:
        """One search is billed exactly when `organic_success` is true."""
        success = raw.json().get("organic_success")
        if success is None:
            return None
        return 1.0 if success else 0.0
