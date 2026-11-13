"""The registry of everything a report states: each check, each metric, each method.

This is the single place where a number's meaning is written down. The report, the logs,
the CI job summary and the README section "How each number is computed" are all generated
from it, so the documentation cannot drift away from the code.
"""

from __future__ import annotations

from dataclasses import dataclass

from serp_bench.models import CLIENT_LAG_THRESHOLD_MS
from serp_bench.stats import MIN_N_FOR_QUANTILE


@dataclass(frozen=True)
class Check:
    """One step of deciding whether a request was delivered."""

    id: str
    name: str
    definition: str
    fails_as: str  # the outcome recorded when this check is the first to fail


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    definition: str  # one line, plain language
    formula: str


@dataclass(frozen=True)
class Method:
    id: str
    name: str
    definition: str


# The step-1 checks, in the order they run. The outcome of a request is the first check
# that fails; a request that passes all of them is `delivered`.
CHECKS: tuple[Check, ...] = (
    Check(
        "dispatched",
        "Sent",
        "The request was sent. It is not sent when the provider does not offer what the task "
        "needs, when too many of our own requests to that provider were still in flight, or "
        "when the run was aborted.",
        "void (not_supported, client_backpressure or run_aborted)",
    ),
    Check(
        "response_received",
        "Response received",
        "A complete response arrived within the time limit. No retry is made.",
        "timeout or network",
    ),
    Check(
        "account_ok",
        "Our account is in order",
        "The response is not about our own key or credit: not HTTP 401, 402 or 403, and not "
        "the provider's way of saying our quota is used up.",
        "void (auth_or_credits)",
    ),
    Check(
        "http_2xx",
        "HTTP 2xx",
        "The status code is 2xx. Redirects are not followed.",
        "http_429, http_4xx, http_5xx or provider_error (1xx, 3xx)",
    ),
    Check(
        "json_body",
        "JSON body",
        "The body is valid JSON.",
        "invalid_json",
    ),
    Check(
        "no_provider_error",
        "No error reported",
        "The body does not report a failure in the provider's own terms.",
        "provider_error",
    ),
    Check(
        "parsed",
        "Documented shape",
        "The body has the structure the provider documents, so the adapter can read it.",
        "malformed_results",
    ),
    Check(
        "has_results",
        "Has results",
        "There is at least one organic result.",
        "empty_results",
    ),
    Check(
        "min_organic",
        "At least three results",
        "There are at least 3 organic results.",
        "empty_results",
    ),
    Check(
        "well_formed_results",
        "Results are well formed",
        "At least 3 organic results have a non-empty title and an absolute http(s) URL. "
        "A result without them stays in the record, marked, and is judged by later steps; "
        "one broken result does not fail an otherwise usable page.",
        "malformed_results",
    ),
    Check(
        "params_echo",
        "Answered what was asked",
        "When the provider says which query and country it used, they are the ones asked "
        "for. A provider that does not say passes.",
        "param_mismatch",
    ),
)

METRICS: tuple[Metric, ...] = (
    Metric(
        "tasks",
        "Tasks",
        "Requests that count: every request of the run except warm-ups, control repeats "
        "and void ones.",
        "records - warm-ups - void",
    ),
    Metric(
        "void",
        "Void",
        "Requests excluded because the problem was ours: our key or credit, a task the "
        "provider does not offer, our own backpressure, an aborted run. Counted, shown, "
        "and left out of every share.",
        "count of records with outcome = void, by reason",
    ),
    Metric(
        "client_lag",
        "Sent late",
        f"Tasks our stand sent more than {CLIENT_LAG_THRESHOLD_MS:.0f} ms after their "
        "scheduled time. They stay in every figure; the count shows how punctual the run was.",
        f"count of Tasks with dispatch_lag_ms > {CLIENT_LAG_THRESHOLD_MS:.0f}",
    ),
    Metric(
        "delivered",
        "Delivered",
        "A usable answer came back on the first attempt: every step-1 check passed.",
        "records with outcome = delivered / Tasks",
    ),
    Metric(
        "failures",
        "Failed, by class",
        "Requests that were not delivered, by the first check that failed.",
        "records with outcome = <class> / Tasks",
    ),
    Metric(
        "delivered_within",
        "Delivered within T seconds",
        "A usable answer came back within T seconds. Slow answers and failures both count "
        "against it.",
        "delivered records with total_ms <= T x 1000 / Tasks",
    ),
    Metric(
        "latency_delivered",
        "Latency, delivered only",
        "How long delivered answers took, from sending the request to the last byte of the "
        "body. Failures are not in this figure, so read it next to Delivered.",
        "quantile of total_ms over delivered records",
    ),
    Metric(
        "time_to_answer",
        "Time to a delivered answer, all tasks",
        "The shortest wait after which at least that share of ALL tasks had a usable answer, "
        "a failure counting as never answered. It is 'not reached' when fewer tasks than "
        "that were delivered, and it always agrees with the deadline shares: p95 is at "
        "most 30 s exactly when at least 95% of all tasks were delivered within 30 s.",
        "inverse of the empirical distribution of total_ms over Tasks, with total_ms = "
        "infinity for a failure",
    ),
    Metric(
        "units_reported",
        "Units the responses say were charged",
        "The sum of what each response itself reports as charged, where it says. This is "
        "measured, as opposed to the prices a provider declares.",
        "sum of billed_units over records where it is stated",
    ),
)

METHODS: tuple[Method, ...] = (
    Method(
        "wilson",
        "Interval of a share",
        "Every share is printed with a Wilson 95% score interval. The intervals assume "
        "independent requests; cluster-aware intervals arrive with the statistics module.",
    ),
    Method(
        "quantile",
        "Quantiles",
        "Latency percentiles of delivered answers use Hyndman and Fan's type 7 (linear "
        "interpolation between the two nearest values, the default of NumPy and R). The "
        "all-tasks view contains 'never', which cannot be interpolated, so it uses the "
        "inverse of the empirical distribution (type 1): the smallest observed value that "
        "at least p% of the tasks did not exceed.",
    ),
    Method(
        "min_n",
        "Enough data for a quantile",
        "A quantile is printed only with enough values behind it: "
        + ", ".join(f"p{p} needs {n}" for p, n in MIN_N_FOR_QUANTILE.items())
        + ". Otherwise the report says n/a and how many values there were.",
    ),
    Method(
        "first_attempt",
        "One attempt",
        "Every figure describes single calls. The benchmark does not retry; a provider's "
        "own retries inside one call are part of what is measured.",
    ),
)

_CHECK = {check.id: check for check in CHECKS}
_METRIC = {metric.id: metric for metric in METRICS}
_METHOD = {method.id: method for method in METHODS}


def check(check_id: str) -> Check:
    return _CHECK[check_id]


def metric(metric_id: str) -> Metric:
    return _METRIC[metric_id]


def method(method_id: str) -> Method:
    return _METHOD[method_id]


def to_markdown() -> str:
    """The registry as Markdown. `serp-bench metrics` prints it, and the README section
    "How each number is computed" is this text."""
    lines = [
        "### Step 1: was a usable answer delivered?",
        "",
        "The checks run in this order. The first one that fails names the outcome; a "
        "request that passes all of them is `delivered`.",
        "",
        "| # | Check | What it verifies | Outcome when it fails |",
        "|---|---|---|---|",
    ]
    for number, item in enumerate(CHECKS, start=1):
        lines.append(f"| {number} | `{item.id}` | {item.definition} | {item.fails_as} |")
    lines += ["", "### Metrics", "", "| Metric | Meaning | Formula |", "|---|---|---|"]
    for item in METRICS:
        lines.append(f"| {item.name} | {item.definition} | `{item.formula}` |")
    lines += ["", "### Methods", ""]
    for item in METHODS:
        lines.append(f"- **{item.name}.** {item.definition}")
    return "\n".join(lines)
