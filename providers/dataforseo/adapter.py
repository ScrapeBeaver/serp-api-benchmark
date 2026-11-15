"""DataForSEO: POST /v3/serp/google/organic/live/regular (Live mode, one task per call).

Documentation:
https://docs.dataforseo.com/v3/serp/google/organic/live/regular/
https://docs.dataforseo.com/v3/appendix/errors/
"""

from __future__ import annotations

import base64

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

LOGIN = "DATAFORSEO_LOGIN"
PASSWORD = "DATAFORSEO_PASSWORD"

OK = 20000
NO_RESULTS = 40102  # "No Search Results": the search returned nothing
# Our own account, not the service: not authorised, the account is not verified yet,
# payment required, a spending limit we set ourselves, no funds.
ACCOUNT_CODES = frozenset({40100, 40104, 40200, 40203, 40210})

# A country is a location code: 2000 plus its ISO 3166-1 numeric code (2840 is the United
# States). The table covers the countries of the query list and their neighbours in it; a
# country that is not here is sent by its two-letter code as a name, which DataForSEO
# refuses with a message that names the field.
ISO_NUMERIC = {
    "AR": 32, "AT": 40, "AU": 36, "BE": 56, "BR": 76, "CA": 124, "CH": 756, "CL": 152,
    "CO": 170, "CZ": 203, "DE": 276, "DK": 208, "ES": 724, "FI": 246, "FR": 250, "GB": 826,
    "GR": 300, "HU": 348, "ID": 360, "IE": 372, "IL": 376, "IN": 356, "IT": 380, "JP": 392,
    "KR": 410, "MX": 484, "MY": 458, "NL": 528, "NO": 578, "NZ": 554, "PH": 608, "PL": 616,
    "PT": 620, "RO": 642, "SA": 682, "SE": 752, "SG": 702, "TH": 764, "TR": 792, "TW": 158,
    "UA": 804, "US": 840, "VN": 704, "ZA": 710, "AE": 784, "EG": 818, "NG": 566, "PK": 586,
}  # fmt: skip
COUNTRY_OF = {2000 + number: code for code, number in ISO_NUMERIC.items()}


def _task(raw: RawResponse) -> dict:
    """The single task of a Live answer; empty when the answer has none."""
    tasks = raw.json().get("tasks") or []
    return tasks[0] if tasks else {}


class DataForSEO(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        search: dict[str, object] = {
            "keyword": task.query,
            "language_code": task.locale.split("-")[0].lower(),
        }
        if task.location:
            search["location_name"] = task.location  # "London,England,United Kingdom"
        elif task.country.upper() in ISO_NUMERIC:
            search["location_code"] = 2000 + ISO_NUMERIC[task.country.upper()]
        else:
            search["location_name"] = task.country.upper()
        if task.device is Device.mobile:
            search["device"] = "mobile"
        if task.page > 1:
            # Later pages come in the same request: depth 20 is the first two pages.
            search["depth"] = task.page * 10
        token = base64.b64encode(f"{self.secret(LOGIN)}:{self.secret(PASSWORD)}".encode()).decode()
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}/v3/serp/google/organic/live/regular",
            headers={"Authorization": f"Basic {token}"},
            json_body=[search],  # an array of tasks; a Live call takes exactly one
        )

    def parse(self, raw: RawResponse) -> Serp:
        task = _task(raw)
        result = (task.get("result") or [{}])[0] or {}
        asked = task.get("data") or {}
        # With a depth above 10 the answer holds every page up to the one asked for; the
        # page asked for is the last, and only its results are this search's.
        wanted = max(1, int(asked.get("depth") or 10) // 10)
        organic = [
            OrganicResult(
                position=int(item["rank_group"]),
                url=item["url"],
                title=item["title"],
                snippet=item.get("description"),
                displayed_url=item.get("breadcrumb"),
            )
            for item in result.get("items") or []
            if item.get("type") == "organic" and int(item.get("page") or 1) == wanted
        ]
        # `spell` is null unless Google answered for another query than the one asked.
        spell = result.get("spell") or {}
        substitutes = ("no_results_found_for", "showing_results_for")
        return Serp(
            organic=organic,
            provider_request_id=task.get("id"),
            substituted=spell.get("type") in substitutes,
            echo=Echo(
                query=result.get("keyword"),
                country=COUNTRY_OF.get(result.get("location_code")),
                locale=result.get("language_code"),
            ),
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """Every answer carries DataForSEO's own status: 20000 is success, both for the
        call and for its task. 40102 is a search with no results; anything else is a
        failure the service reports itself."""
        body = raw.json()
        task = _task(raw)
        if body.get("status_code") == OK and not task:
            # "Ok." with no task in it: seen on a live call. Not a page with no results,
            # and not an answer either.
            return FailureClass.provider_error
        code = task.get("status_code", body.get("status_code"))
        if code == OK:
            return None
        if code == NO_RESULTS:
            return FailureClass.empty_results
        return FailureClass.provider_error

    def own_fault(self, raw: RawResponse) -> str | None:
        """A refused login, an empty balance or our own spending limit, when it comes back
        inside a 2xx answer instead of as HTTP 401 or 402."""
        try:
            body = raw.json()
            codes = {body.get("status_code"), _task(raw).get("status_code")}
        except (ValueError, AttributeError):
            return None
        return VOID_AUTH_OR_CREDITS if codes & ACCOUNT_CODES else None

    def billed_units(self, raw: RawResponse) -> float | None:
        """The task's `cost` in dollars. A refund made later does not show here."""
        cost = _task(raw).get("cost")
        return None if cost is None else float(cost)
