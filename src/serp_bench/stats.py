"""The statistical methods a report uses. Pure functions, no dependencies.

Both are stated once here and nowhere else, so every number in a report is produced by a
method anyone can look up and check.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Z_95 = 1.959963984540054  # the 97.5th percentile of the standard normal distribution

# A quantile is only printed when there are at least this many values behind it. Below
# that, the tail it describes is a handful of requests and the figure would mislead.
# The tail percentiles start at 50 values so that they appear within the first days of a
# provider: at 50 values p95 rests on the three slowest answers and p99 on the slowest
# two, so both are rough until there are a few hundred.
MIN_N_FOR_QUANTILE = {50: 20, 90: 50, 95: 50, 99: 50}


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """The Wilson score interval for a share `successes / n` (95% by default).

    Unlike the normal ("Wald") interval it stays inside [0, 1] and does not collapse to a
    point when the share is 0% or 100%, which is exactly where delivery rates live.
    Returns None when n is 0.
    """
    if n <= 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = (z / (1 + z2 / n)) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high


def quantile(values: Sequence[float], p: float) -> float | None:
    """The p-th percentile (0 <= p <= 100) by Hyndman and Fan's type 7: linear interpolation
    between the two nearest order statistics. This is the default of NumPy and R.

    `math.inf` is a legal value and means "never": when the percentile falls on one, the
    result is `math.inf`. Returns None for an empty sequence.
    """
    if not values:
        return None
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * p / 100
    lower = math.floor(position)
    fraction = position - lower
    if fraction == 0:
        return ordered[lower]
    upper = ordered[lower + 1]
    if math.isinf(upper):
        return math.inf
    return ordered[lower] + fraction * (upper - ordered[lower])


def quantile_ecdf(values: Sequence[float], p: float) -> float | None:
    """The p-th percentile (0 < p <= 100) as the inverse of the empirical distribution
    (Hyndman and Fan's type 1): the smallest value v such that at least p% of the values
    are <= v. No interpolation.

    Used where some values are `math.inf` ("never answered"). It then reads as "the
    shortest wait after which at least p% of all tasks had their answer", and it agrees
    exactly with a share counted against a deadline: the result is <= T precisely when at
    least p% of the values are <= T. Interpolating towards infinity cannot give that.
    Returns None for an empty sequence.
    """
    if not values:
        return None
    if not 0 < p <= 100:
        raise ValueError("p must be above 0 and at most 100")
    ordered = sorted(values)
    # the k-th smallest value, k = ceil(n * p / 100); the tiny slack absorbs float error
    # in n * p / 100 so that an exact integer does not round up to the next rank
    rank = math.ceil(len(ordered) * p / 100 - 1e-9)
    return ordered[max(rank, 1) - 1]
