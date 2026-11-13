"""The worked example contracts in providers/_examples: each one validates and prices."""

from __future__ import annotations

from pathlib import Path

import pytest

from serp_bench.pricing import HEADLINE_VOLUMES, PriceQuote, Shape, Unknown, price_curve
from serp_bench.provider import parse_config

EXAMPLES = sorted((Path(__file__).parent.parent / "providers" / "_examples").glob("*.yaml"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_example_validates_and_prices(path):
    config = parse_config(path)
    assert config.id == path.stem
    assert config.status == "draft"
    billing = config.current_billing
    assert all(source.startswith("https://") for source in billing.sources)
    points = price_curve(Shape(), billing, list(HEADLINE_VOLUMES))
    assert len(points) == len(HEADLINE_VOLUMES)
    quotes = [point.quote for point in points]
    assert all(isinstance(quote, PriceQuote | Unknown) for quote in quotes)
    # Every example has a published price at the smallest headline volume at least.
    assert isinstance(quotes[0], PriceQuote), quotes[0]
    assert quotes[0].price_per_1k > 0
