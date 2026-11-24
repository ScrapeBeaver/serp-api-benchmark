"""Bright Data: POST /request to a SERP API zone, parsed JSON.

Documentation:
https://docs.brightdata.com/api-reference/rest-api/serp/serp-api
https://docs.brightdata.com/scraping-automation/serp-api/query-parameters/google
"""

from __future__ import annotations

from urllib.parse import urlencode

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

API_KEY = "BRIGHTDATA_API_KEY"
ZONE = "BRIGHTDATA_ZONE"
GOOGLE = "https://www.google.com/search"
DEBUG_HEADER = "x-brd-debug"  # carries `billed=true|false`


def _header(raw: RawResponse, name: str) -> str | None:
    for key, value in raw.headers.items():
        if key.lower() == name:
            return value
    return None


class BrightData(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        # The request names a Google URL; everything about the search is in its query
        # string. `q` goes first: "arguments placed before it can slow responses".
        search: dict[str, str] = {
            "q": task.query,
            "gl": task.country.lower(),
            "hl": task.locale.split("-")[0].lower(),  # "two-letter language code"
            "brd_json": "1",  # parsed JSON
        }
        if task.device is Device.mobile:
            search["brd_mobile"] = "1"  # "random mobile user-agent"
        if task.page > 1:
            search["start"] = str((task.page - 1) * 10)  # start=10 is page 2
        if task.location:
            search["uule"] = task.location  # the canonical name itself, not its encoded form
        if task.time_range == "week":
            search["tbs"] = "qdr:w"  # the address is Google's own: its past-week filter
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/request",
            headers={"Authorization": f"Bearer {self.secret(API_KEY)}"},
            json_body={
                "zone": self.secret(ZONE),
                "url": f"{GOOGLE}?{urlencode(search)}",
                "format": "raw",  # the body is the parsed page itself, with no envelope
            },
        )

    def parse(self, raw: RawResponse) -> Serp:
        data = raw.json()
        organic = [
            OrganicResult(
                position=int(item["rank"]),
                url=item["link"],
                title=item["title"],
                snippet=item.get("description"),
                displayed_url=item.get("display_link"),
            )
            for item in data["organic"]
        ]
        # `general` says what the page was for: the query, `gl` and the language.
        general = data.get("general") or {}
        return Serp(
            organic=organic,
            ai_overview=read_ai_overview(data.get("ai_overview")),
            provider_request_id=(data.get("input") or {}).get("request_id"),
            echo=Echo(
                query=general.get("query"),
                country=general.get("gl"),
                locale=general.get("language"),
            ),
        )

    def billed_units(self, raw: RawResponse) -> float | None:
        """One request, when the debug header says it was billed; zero when it says it was
        not; unknown when the header is not there."""
        debug = (_header(raw, DEBUG_HEADER) or "").lower()
        if "billed=true" in debug:
            return 1.0
        if "billed=false" in debug:
            return 0.0
        return None
