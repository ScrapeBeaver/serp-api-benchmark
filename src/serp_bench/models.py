"""The data that moves through a run: a task goes out, a raw response comes back,
an adapter turns it into a normalised SERP, and one record per (task, provider) is kept.

Records are the durable contract. Everything the benchmark reports is computed from them,
so a record has to carry enough to recompute a report, and nothing that would republish a
provider's output (no titles, no snippets, no bodies).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

OUTCOME_DELIVERED = "delivered"
OUTCOME_VOID = "void"

# Void reasons. A void record is our problem, not the provider's: it counts neither for
# nor against anyone.
VOID_AUTH_OR_CREDITS = "auth_or_credits"
VOID_NOT_SUPPORTED = "not_supported"
VOID_RUN_ABORTED = "run_aborted"
VOID_CLIENT_BACKPRESSURE = "client_backpressure"

# A request sent more than this long after its scheduled time is flagged `client_lag`:
# our own stand was late, and the report says so.
CLIENT_LAG_THRESHOLD_MS = 250.0


class Device(StrEnum):
    desktop = "desktop"
    mobile = "mobile"


class FailureClass(StrEnum):
    """Why a request was not delivered. Exactly one per failed request."""

    timeout = "timeout"
    network = "network"
    http_429 = "http_429"
    http_4xx = "http_4xx"
    http_5xx = "http_5xx"
    invalid_json = "invalid_json"
    provider_error = "provider_error"
    empty_results = "empty_results"
    malformed_results = "malformed_results"
    param_mismatch = "param_mismatch"


class Task(BaseModel):
    """One search to send: the unit every provider receives identically."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    pool: str
    headline: bool
    query_id: str
    query: str
    country: str  # ISO 3166-1 alpha-2, upper case
    locale: str  # language, e.g. "en" or "de"
    device: Device = Device.desktop
    page: int = 1  # 1-based results page
    # A place below country level, as Google's canonical name ("Austin,Texas,United States").
    location: str | None = None


class HttpRequest(BaseModel):
    """What an adapter asks the runner to send. The runner owns the connection and the clock."""

    method: Literal["GET", "POST"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    json_body: Any | None = None


class RawResponse(BaseModel):
    """What came back, with the runner's timings. `error` is set when nothing usable came back."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    ttfb_ms: float | None = None  # request sent -> response headers received
    total_ms: float | None = None  # request sent -> last byte of the body
    error: Literal["timeout", "network"] | None = None
    error_detail: str | None = None

    def json(self) -> Any:  # type: ignore[override]
        """The body as JSON. Raises ValueError when it is not JSON."""
        return json.loads(self.body.decode("utf-8"))


class OrganicResult(BaseModel):
    position: int
    url: str
    title: str
    snippet: str | None = None
    displayed_url: str | None = None


class Echo(BaseModel):
    """The parameters a provider says it used, when its response says so."""

    query: str | None = None
    country: str | None = None
    locale: str | None = None


class AiOverview(BaseModel):
    """Google's AI Overview as an answer carried it, reduced to what can be compared.
    See `serp_bench.ai_overview`."""

    chars: int  # characters of the overview's own text; sources, addresses and ids left out
    deferred: bool = False  # no text, but a token or a link for a second request


class Serp(BaseModel):
    """A provider's answer in the shape every provider is reduced to."""

    organic: list[OrganicResult] = Field(default_factory=list)
    provider_request_id: str | None = None
    echo: Echo = Field(default_factory=Echo)
    # True when the provider itself says the results are not for the query as asked (Google
    # found nothing for it and shows results for something close). None: it does not say.
    substituted: bool | None = None
    # The AI Overview block of the answer. None: the answer has no such block.
    ai_overview: AiOverview | None = None


class CheckResult(BaseModel):
    """One step-1 check as it went for one request. See `serp-bench metrics`."""

    check: str
    passed: bool
    detail: str


class ResultRef(BaseModel):
    """One organic result as a record keeps it: where it points, not what it says."""

    pos: int
    host: str  # lower case, without a leading "www."; "" when the link is not an absolute URL
    url_hash: str | None  # first 16 hex of sha256 of the normalised URL; None when not absolute
    # The host is a Google domain. True for a redirect wrapper, and also for a real page on
    # a Google site (support.google.com), which is a legitimate result.
    on_google: bool
    # The link is Google's own redirect or results page instead of the destination, e.g.
    # google.com/goto?url=... or a relative /url?q=... This is the unresolved-link signal.
    wrapper: bool = False
    # The result has a non-empty title and an absolute http(s) URL.
    well_formed: bool = True


class Record(BaseModel):
    """One (task, provider) outcome. See README, "The record format"."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    config_hash: str
    methodology_version: str
    pool: str
    headline: bool
    task_id: str
    query_id: str
    query: str
    country: str
    locale: str
    device: Device
    page: int
    location: str | None = None
    run_index: int | None = None  # which of the day's runs; None outside a scheduled run
    provider: str
    provider_status: str  # draft | conformance | shadow | published, at the time of the run
    adapter_version: str
    scheduled_at: str | None  # UTC, ISO 8601: when the timetable said to send it
    started_at: str | None  # UTC, ISO 8601: when it was sent; None when it was not
    dispatch_lag_ms: float | None  # started minus scheduled
    client_lag: bool = False  # dispatch_lag_ms above the threshold: our stand was late
    ttfb_ms: float | None
    total_ms: float | None
    http_status: int | None
    bytes: int | None
    outcome: str  # "delivered" | a FailureClass value | "void"
    void_reason: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)  # the trail that led to `outcome`
    organic_count: int | None = None
    results: list[ResultRef] = Field(default_factory=list)
    provider_request_id: str | None = None
    billed_units: float | None = None
    # The provider's own statement that the results are substitutes for the query asked.
    substituted: bool | None = None
    # The AI Overview block of the answer: how much text it had. None: no such block, or a
    # record written before the block was looked at.
    ai_overview: AiOverview | None = None
    warmup: bool = False
    # A control repeat: the same task sent to the same provider once more, after the run's
    # last measured request, because its first page did not match the other providers'.
    # It serves the step-3 check only and, like a warm-up, is never counted.
    control: bool = False

    @property
    def counts(self) -> bool:
        """Whether this request is one of the measured ones (not a warm-up, not a control)."""
        return not (self.warmup or self.control)


VALID_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_DELIVERED, OUTCOME_VOID, *(f.value for f in FailureClass)}
)
