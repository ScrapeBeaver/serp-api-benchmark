"""ScrapingBee: GET /api/v1/google (Google Search API, light request).

Documentation: https://www.scrapingbee.com/documentation/google-api/
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from serp_bench.ai_overview import read as read_ai_overview
from serp_bench.models import Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider

API_KEY = "SCRAPINGBEE_API_KEY"
COST_HEADER = "spb-cost"  # the credits this request cost, as the general API documents it
REQUEST_ID_HEADER = "spb-request-id"


def _header(raw: RawResponse, wanted: str) -> str | None:
    return next((value for name, value in raw.headers.items() if name.lower() == wanted), None)


class ScrapingBee(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        params = {
            "search": task.query,
            "country_code": task.country.lower(),
            "device": task.device.value,
            # There is no language parameter. `locale` inside `extra_params` sets the
            # language of Google's interface, which is what the other providers' language
            # parameter does as well.
            "extra_params": f"locale={task.locale}",
        }
        if task.page > 1:
            params["page"] = str(task.page)
        # `light_request` is left at its default, true: the cheap mode is the one measured.
        return HttpRequest(
            method="GET",
            url=f"{self.base_url}/api/v1/google",
            headers={"Authorization": f"Bearer {self.secret(API_KEY)}"},
            params=params,
        )

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["position"]),
                url=item["url"],
                title=item["title"],
                snippet=item.get("description"),
                displayed_url=item.get("displayed_url"),
            )
            for item in data["organic_results"]
        ]
        # The response does not name the query and country it used; it gives the Google URL
        # it fetched, which carries both.
        meta = data.get("meta_data") or {}
        used = parse_qs(urlparse(meta.get("url") or "").query)
        return Serp(
            organic=organic,
            ai_overview=read_ai_overview(data.get("ai_overviews")),
            provider_request_id=_header(raw, REQUEST_ID_HEADER),
            # `zero_results_for_original_query`: Google found nothing for the query as asked.
            substituted=meta.get("zero_results_for_original_query"),
            echo=Echo(
                query=(used.get("q") or [None])[0],
                country=(used.get("gl") or [None])[0],
                locale=(used.get("hl") or [None])[0],
            ),
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        """Credits charged, from the `Spb-cost` response header, when it is there."""
        value = _header(raw, COST_HEADER)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None
