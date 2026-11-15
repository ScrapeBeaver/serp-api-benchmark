"""ScraperAPI: GET /structured/google/search (Structured Data Endpoint), parsed JSON.

Documentation:
https://docs.scraperapi.com/structured-data-endpoints/search-and-insights/google/google-serp-api
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from serp_bench.models import Echo, HttpRequest, OrganicResult, RawResponse, Serp, Task
from serp_bench.provider import Provider
from serp_bench.uule import uule

API_KEY = "SCRAPERAPI_API_KEY"
COST_HEADER = "sa-credit-cost"  # the credits this request cost
FINAL_URL_HEADER = "sa-final-url"  # the Google address the request ended on


def _header(raw: RawResponse, wanted: str) -> str | None:
    return next((value for name, value in raw.headers.items() if name.lower() == wanted), None)


class ScraperApi(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        params = {
            # The key travels in the address: the endpoint documents no header for it. The
            # runner logs requests through `loggable`, which removes it.
            "api_key": self.secret(API_KEY),
            "query": task.query,
            # `gl` and `hl` are Google's own country and language. `country_code`, the
            # country the request is sent from, is optional and left unset: single
            # countries are sold from the Business plan up, and when it is set the service
            # also adds a city of its own choosing to the search and switches the Google
            # domain. Without it the address asked for is google.com with `gl` and `hl`,
            # the same search every other provider is given.
            "gl": task.country.lower(),
            "hl": task.locale.split("-")[0].lower(),
        }
        if task.page > 1:
            params["start"] = str((task.page - 1) * 10)  # an offset
        if task.location:
            params["uule"] = uule(task.location)
        return HttpRequest(
            method="GET", url=f"{self.base_url}/structured/google/search", params=params
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
            # A section with nothing in it is left out of the answer altogether, so an
            # answer without `organic_results` is a page without organic results, which
            # the shared checks then count as a failure: empty results.
            for item in data.get("organic_results", [])
        ]
        # The body does not repeat the country and language it used; the `sa-final-url`
        # header gives the Google address the request ended on, which carries them.
        used = parse_qs(urlparse(_header(raw, FINAL_URL_HEADER) or "").query)
        shown = (data.get("search_information") or {}).get("query_displayed")
        return Serp(
            organic=organic,
            echo=Echo(
                query=(used.get("q") or [shown])[0],
                country=(used.get("gl") or [None])[0],
                locale=(used.get("hl") or [None])[0],
            ),
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        """Credits charged, from the `sa-credit-cost` response header, when it is there."""
        value = _header(raw, COST_HEADER)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None
