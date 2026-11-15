"""The two statistical methods, against published values."""

from __future__ import annotations

import math

import pytest

from serp_bench.stats import MIN_N_FOR_QUANTILE, quantile, quantile_ecdf, wilson_interval


# Newcombe (1998), "Two-sided confidence intervals for the single proportion",
# Statistics in Medicine 17, Table I, method 3 (Wilson score, no continuity correction).
@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0000, 0.1611),
        (1, 29, 0.0061, 0.1718),
    ],
)
def test_wilson_matches_newcombe(successes, n, low, high):
    interval = wilson_interval(successes, n)
    assert interval == pytest.approx((low, high), abs=5e-5)


def test_wilson_does_not_collapse_at_the_edges():
    assert wilson_interval(20, 20) == pytest.approx((0.8389, 1.0), abs=5e-5)
    low, high = wilson_interval(0, 5)
    assert low == 0.0 and 0.4 < high < 0.5
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(1 - high)  # symmetric around one half


def test_wilson_without_data_and_with_bad_input():
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


# Values of numpy.percentile(x, p) with its default method ("linear", Hyndman-Fan type 7).
@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([1, 2, 3, 4], 50, 2.5),
        ([1, 2, 3, 4], 90, 3.7),
        ([1, 2, 3, 4], 25, 1.75),
        ([15, 20, 35, 40, 50], 40, 29.0),
        ([15, 20, 35, 40, 50], 95, 48.0),
        ([15, 20, 35, 40, 50], 0, 15.0),
        ([15, 20, 35, 40, 50], 100, 50.0),
        ([7], 99, 7.0),
        ([3, 1, 2], 50, 2.0),  # order of input does not matter
    ],
)
def test_quantile_is_type_7(values, p, expected):
    assert quantile(values, p) == pytest.approx(expected)


def test_quantile_of_nothing_is_none():
    assert quantile([], 50) is None
    with pytest.raises(ValueError):
        quantile([1], 101)


def test_quantile_with_never():
    """`inf` means a task that was never answered."""
    values = [1.0, 2.0, 3.0, math.inf]  # three of four answered
    assert quantile(values, 50) == pytest.approx(2.5)
    assert quantile(values, 2 / 3 * 100) == pytest.approx(3.0)
    assert quantile(values, 90) == math.inf
    assert quantile([math.inf, math.inf], 50) == math.inf


def test_minimum_sample_sizes():
    assert MIN_N_FOR_QUANTILE == {50: 20, 90: 50, 95: 50, 99: 50}


# --- the all-tasks view: inverse of the empirical distribution -----------------------------


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([1, 2, 3, 4], 50, 2),  # at least half of the values are <= 2
        ([1, 2, 3, 4], 51, 3),
        ([1, 2, 3, 4], 100, 4),
        ([5], 50, 5),
        ([3, 1, 2], 1, 1),
    ],
)
def test_quantile_ecdf_is_the_smallest_value_covering_p_percent(values, p, expected):
    assert quantile_ecdf(values, p) == expected


def test_quantile_ecdf_is_reached_exactly_when_enough_tasks_were_delivered():
    """90 of 100 delivered: p90 is the slowest delivered answer, p91 is never reached.
    Interpolation (type 7) would call p90 unreached here, contradicting the 90% share."""
    values = [float(i) for i in range(1, 91)] + [math.inf] * 10
    assert quantile_ecdf(values, 90) == 90.0
    assert quantile_ecdf(values, 91) == math.inf
    assert quantile(values, 90) == math.inf  # why type 7 is not used for this view


@pytest.mark.parametrize("delivered", [0, 1, 37, 94, 95, 96, 100])
def test_quantile_ecdf_agrees_with_a_share_against_a_deadline(delivered):
    """p95 <= T exactly when at least 95% of all tasks were delivered within T."""
    deadline = 30.0
    values = [1.0] * delivered + [math.inf] * (100 - delivered)
    share_within = sum(v <= deadline for v in values) / len(values)
    assert (quantile_ecdf(values, 95) <= deadline) == (share_within >= 0.95)


def test_quantile_ecdf_edges():
    assert quantile_ecdf([], 50) is None
    with pytest.raises(ValueError):
        quantile_ecdf([1], 0)
