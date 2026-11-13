"""Step 1 of the funnel: was a usable answer delivered?

This module decides the outcome of one (task, provider) request. The rules are mechanical
and written here once; nothing about an outcome is decided by hand.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from serp_bench.models import (
    OUTCOME_DELIVERED,
    OUTCOME_VOID,
    VOID_AUTH_OR_CREDITS,
    CheckResult,
    FailureClass,
    RawResponse,
    ResultRef,
    Serp,
    Task,
)
from serp_bench.provider import Provider

MIN_ORGANIC_RESULTS = 3

# Query parameters that identify a click or a campaign, not a page.
_TRACKING_PARAMS = frozenset({"gclid", "fbclid", "msclkid", "srsltid", "dclid", "yclid"})
_GOOGLE_HOST = re.compile(r"(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$")
# Google Search itself, as opposed to another Google site such as support.google.com.
_GOOGLE_SEARCH_HOST = re.compile(r"^(www\.)?google\.[a-z]{2,3}(\.[a-z]{2})?$")
# First path segments of Google's redirects, results pages and interstitials.
_WRAPPER_PATHS = frozenset({"goto", "url", "aclk", "search", "sorry", "interstitial", "imgres"})
_SHARE_HOSTS = frozenset({"share.google"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


# --- URLs -------------------------------------------------------------------------------


def is_absolute_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def host_of(url: str) -> str:
    """The host in lower case, without a leading `www.`."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_google_host(host: str) -> bool:
    """True for google.com and its country domains and subdomains. A result that points
    there is Google's own redirect wrapper or page, not a destination."""
    return bool(_GOOGLE_HOST.search(host))


def _is_wrapper_path(path: str) -> bool:
    return path.lstrip("/").split("/", 1)[0].lower() in _WRAPPER_PATHS


def is_google_wrapper(url: str) -> bool:
    """True when a result link is Google's own redirect or page instead of the destination.

    Google no longer puts a result's address in its markup; it puts an opaque redirect
    (`/goto?url=...`). A provider that passes that through hands its customer a google.com
    address for the result. This recognises those: a link on Google Search's own host, or a
    relative link, whose path is a redirect, a results page or an interstitial; and Google's
    share short links. A real page on another Google site (support.google.com,
    play.google.com) is a destination and is not a wrapper.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if not parts.scheme and not parts.netloc:
        return url.startswith("/") and _is_wrapper_path(parts.path)
    host = (parts.hostname or "").lower()
    if host in _SHARE_HOSTS:
        return True
    return bool(_GOOGLE_SEARCH_HOST.match(host)) and _is_wrapper_path(parts.path)


def normalise_url(url: str) -> str:
    """The form of a URL two providers agree on when they mean the same page:
    scheme and host in lower case, no leading `www.`, no default port, no fragment,
    no click or campaign parameters, no trailing slash on the path."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = host_of(url)
    port = parts.port
    netloc = host if port is None or _DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        ]
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """First 16 hex characters of the SHA-256 of the normalised URL."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()[:16]


def result_refs(serp: Serp) -> list[ResultRef]:
    """Every organic result, in order. A result whose link is not an absolute URL is kept
    too, marked, so later steps can count it; dropping it here would hide exactly the
    links a provider failed to resolve."""
    refs = []
    for result in serp.organic:
        absolute = is_absolute_http_url(result.url)
        host = host_of(result.url) if absolute else ""
        refs.append(
            ResultRef(
                pos=result.position,
                host=host,
                url_hash=url_hash(result.url) if absolute else None,
                on_google=absolute and is_google_host(host),
                wrapper=is_google_wrapper(result.url),
                well_formed=absolute and bool(result.title.strip()),
            )
        )
    return refs


# --- the outcome of one request -----------------------------------------------------------


@dataclass(frozen=True)
class Step1:
    outcome: str  # "delivered" | a FailureClass value | "void"
    checks: list[CheckResult]  # the checks that ran, in order; the last one failed unless delivered
    void_reason: str | None = None
    serp: Serp | None = None
    organic_count: int | None = None
    results: list[ResultRef] = field(default_factory=list)


def void_reason(provider: Provider, raw: RawResponse) -> str | None:
    """THE void rule. A void request is our problem, not the provider's, and counts neither
    for nor against anyone:

    * HTTP 401, 402 or 403: our key is wrong, revoked or out of credit;
    * anything the adapter recognises as the same thing in another form.

    Three more void reasons are set by the runner, which is the only place that knows them:
    a task the provider does not offer (`not_supported`), too many of our own requests in
    flight (`client_backpressure`) and an interrupted run (`run_aborted`). A provider with
    no API key configured is not run at all.
    """
    if raw.status in (401, 402, 403):
        return VOID_AUTH_OR_CREDITS
    if raw.error is None:
        try:
            reason = provider.own_fault(raw)
        except Exception:  # noqa: BLE001 - a body the adapter cannot read is judged below
            return None
        # A void request leaves every share, so an adapter may claim it for one reason only:
        # our own key or credit. Anything else it returns is ignored, and the response is
        # judged like any other.
        return reason if reason == VOID_AUTH_OR_CREDITS else None
    return None


def not_sent(reason: str, detail: str) -> Step1:
    """The outcome of a request that was never sent."""
    return Step1(
        OUTCOME_VOID, [CheckResult(check="dispatched", passed=False, detail=detail)], reason
    )


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def _http_failure(status: int) -> FailureClass | None:
    if status == 429:
        return FailureClass.http_429
    if 400 <= status < 500:
        return FailureClass.http_4xx
    if status >= 500:
        return FailureClass.http_5xx
    if not 200 <= status < 300:
        return FailureClass.provider_error  # 1xx, 3xx: redirects are not followed
    return None


def judge(
    provider: Provider,
    task: Task,
    raw: RawResponse,
    *,
    timeout: float | None = None,
    dispatch_lag_ms: float | None = None,
) -> Step1:
    """Decide the outcome of a request that was sent.

    The checks run in the order of `metrics.CHECKS`; the first that fails names the outcome
    and ends the trail. Every check leaves a line saying what it saw, in words, with no
    secret and nothing from the response body in it.
    """
    trail: list[CheckResult] = []

    def passed(check: str, detail: str) -> None:
        trail.append(CheckResult(check=check, passed=True, detail=detail))

    def failed(check: str, detail: str, outcome: str, **more: object) -> Step1:
        trail.append(CheckResult(check=check, passed=False, detail=detail))
        return Step1(str(outcome), trail, **more)  # type: ignore[arg-type]

    lag = "" if dispatch_lag_ms is None else f" {dispatch_lag_ms:.0f} ms after its scheduled time"
    passed("dispatched", f"sent{lag}")

    limit = "the time limit" if timeout is None else f"{timeout:g} s"
    if raw.error == "timeout":
        return failed("response_received", f"no complete response within {limit}", "timeout")
    if raw.error == "network":
        kind = raw.error_detail or "connection error"
        return failed("response_received", f"the connection failed ({kind})", "network")
    passed("response_received", f"complete response in {(raw.total_ms or 0) / 1000:.2f} s")

    status = raw.status or 0
    reason = void_reason(provider, raw)
    if reason is not None:
        detail = f"HTTP {status}: a problem with our own key or credit, not with the service"
        return failed("account_ok", detail, OUTCOME_VOID, void_reason=reason)
    passed("account_ok", "not a key or credit problem")

    http_failure = _http_failure(status)
    if http_failure is not None:
        return failed("http_2xx", f"HTTP {status}", http_failure)
    passed("http_2xx", f"HTTP {status}")

    try:
        raw.json()
    except (ValueError, UnicodeDecodeError):
        return failed("json_body", f"the body ({len(raw.body)} bytes) is not JSON", "invalid_json")
    passed("json_body", f"valid JSON, {len(raw.body)} bytes")

    # An adapter that cannot read a body must cost that one request, never the whole run.
    try:
        reported = provider.classify_error(raw)
    except Exception as exc:  # noqa: BLE001
        detail = f"the body does not have the documented shape ({type(exc).__name__})"
        return failed("no_provider_error", detail, "malformed_results")
    if reported is not None:
        return failed("no_provider_error", "the body reports a failure", reported)
    passed("no_provider_error", "the body reports no failure")

    try:
        serp = provider.parse(raw)
    except Exception as exc:  # noqa: BLE001
        detail = f"the body does not have the documented shape ({type(exc).__name__})"
        return failed("parsed", detail, "malformed_results")
    count = len(serp.organic)
    refs = result_refs(serp)
    seen = {"serp": serp, "organic_count": count, "results": refs}
    passed("parsed", "the body has the documented shape")

    if count == 0:
        return failed("has_results", "0 organic results", "empty_results", **seen)
    passed("has_results", f"{count} organic results")

    if count < MIN_ORGANIC_RESULTS:
        detail = f"{count} organic results, need at least {MIN_ORGANIC_RESULTS}"
        return failed("min_organic", detail, "empty_results", **seen)
    passed("min_organic", f"{count} organic results, at least {MIN_ORGANIC_RESULTS} needed")

    good = sum(1 for ref in refs if ref.well_formed)
    shape = f"{good} of {count} results have a title and an absolute http(s) URL"
    if good < MIN_ORGANIC_RESULTS:
        detail = f"only {shape}, need at least {MIN_ORGANIC_RESULTS}"
        return failed("well_formed_results", detail, "malformed_results", **seen)
    passed("well_formed_results", shape)

    echo = serp.echo
    if echo.query is not None and not _same_text(echo.query, task.query):
        detail = "the provider says it searched a different query than the one asked"
        return failed("params_echo", detail, "param_mismatch", **seen)
    if echo.country is not None and echo.country.casefold() != task.country.casefold():
        used = echo.country.upper() if re.fullmatch(r"[A-Za-z]{2}", echo.country) else "another"
        detail = f"asked for country {task.country}, the provider says it used {used}"
        return failed("params_echo", detail, "param_mismatch", **seen)
    said = [name for name, value in (("query", echo.query), ("country", echo.country)) if value]
    passed(
        "params_echo",
        f"the provider confirms the {' and '.join(said)} asked for"
        if said
        else "the provider does not say which parameters it used",
    )

    return Step1(OUTCOME_DELIVERED, trail, **seen)  # type: ignore[arg-type]
