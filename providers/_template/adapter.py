"""Adapter template. Copy this folder to `providers/<id>/` and fill in the four methods.

An adapter never sends a request and never measures time: it says what to send and reads
what came back. The runner owns the connection, the clock and the timeout, so every
provider is measured the same way.

Checklist
  1. `provider.yaml`: fill every field from the provider's documentation and pricing page.
  2. `build_request`: the provider's cheapest real-time Google endpoint, default settings.
  3. `parse`: reduce the response to `Serp`. Index required fields with `[...]` so that a
     response without them raises and is recorded as `malformed_results`.
  4. `fixtures/`: at least one recorded success and one failure (see providers/README.md).
  5. `uv run pytest tests/test_contract.py` must pass without touching any core file.
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

API_KEY = "EXAMPLE_API_KEY"  # must be listed under auth.env in provider.yaml


class Example(Provider):
    def build_request(self, task: Task) -> HttpRequest:
        """The request for one task. Read secrets with `self.secret(NAME)` only: that is
        what guarantees they are redacted everywhere."""
        return HttpRequest(
            method="GET",
            url=f"{self.base_url}/google",
            params={
                "api_key": self.secret(API_KEY),
                "query": task.query,
                "country": task.country.lower(),
                "language": task.locale,
                "page": str(task.page - 1),
                # task.device is Device.desktop or Device.mobile
            },
        )

    def parse(self, raw: RawResponse) -> Serp:
        """Called only for a 2xx JSON body that `classify_error` accepted."""
        data = raw.json()
        return Serp(
            organic=[
                OrganicResult(
                    position=int(item["position"]),
                    url=item["link"],  # the destination, as the provider returns it
                    title=item["title"],
                    snippet=item.get("snippet"),
                    displayed_url=item.get("displayed_link"),
                )
                for item in data["organic_results"]
            ],
            provider_request_id=None,  # the provider's id for this request, if it gives one
            echo=Echo(),  # query / country / locale the provider says it used, if it says
        )

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """A failure reported inside a 2xx body, or None. Delete if the provider has none."""
        if raw.json().get("error"):
            return FailureClass.provider_error
        return None

    # Optional:
    #
    # def own_fault(self, raw: RawResponse) -> str | None:
    #     """Return models.VOID_AUTH_OR_CREDITS when the response means OUR quota is used
    #     up in a form other than HTTP 401/402/403. Such requests are void."""
    #
    # def billed_units(self, raw: RawResponse) -> float | None:
    #     """Units this response says were charged (a header, a body field), else None."""
