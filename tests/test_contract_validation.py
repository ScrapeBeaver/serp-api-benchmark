"""provider.yaml is strict: what the contract does not define is an error with a path."""

from __future__ import annotations

import copy
import json

import pytest
import yaml
from conftest import CONTRACTS_DIR, PROVIDERS_DIR

from serp_bench.contract import ProviderConfig
from serp_bench.provider import ProviderConfigError, parse_config


def load(name: str) -> dict:
    return yaml.safe_load((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


def error_for(tmp_path, data: dict) -> str:
    path = tmp_path / "provider.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ProviderConfigError) as caught:
        parse_config(path)
    return str(caught.value)


@pytest.mark.parametrize(
    "path",
    sorted(CONTRACTS_DIR.glob("*.yaml")) + sorted(PROVIDERS_DIR.glob("*/provider.yaml")),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_every_shipped_file_is_valid(path):
    config = parse_config(path)
    assert config.billing, "at least one billing entry"
    for entry in config.billing:
        assert entry.sources and entry.checked_at


def test_unknown_key_is_rejected_with_its_path(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["meter"]["colour"] = "blue"
    message = error_for(tmp_path, data)
    assert "billing.0.meter.colour" in message
    assert "Extra inputs are not permitted" in message


def test_unknown_top_level_key_is_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    data["pricing"] = {"unit": "credit"}
    assert "pricing: Extra inputs" in error_for(tmp_path, data)


def test_a_billing_entry_without_sources_is_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    del data["billing"][0]["sources"]
    assert "billing.0.sources" in error_for(tmp_path, data)
    data = load("credit_subscription.yaml")
    data["billing"][0]["sources"] = []
    assert "billing.0.sources" in error_for(tmp_path, data)


def test_a_source_must_be_a_url(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["sources"] = ["their pricing page"]
    assert "billing.0.sources.0" in error_for(tmp_path, data)


def test_billing_entries_out_of_date_order_are_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"].reverse()
    message = error_for(tmp_path, data)
    assert "ascending" in message and "append" in message


def test_two_entries_on_the_same_day_are_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][1]["effective_from"] = data["billing"][0]["effective_from"]
    assert "ascending" in error_for(tmp_path, data)


def test_a_rule_needs_exactly_one_operation(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["request_cost"]["rules"][0]["multiply"] = 2
    assert "exactly one of" in error_for(tmp_path, data)
    data = load("credit_subscription.yaml")
    del data["billing"][0]["request_cost"]["rules"][0]["set"]
    assert "exactly one of" in error_for(tmp_path, data)


def test_a_rule_needs_a_condition_from_the_vocabulary(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["request_cost"]["rules"][0]["when"] = {}
    assert "at least one key" in error_for(tmp_path, data)
    data["billing"][0]["request_cost"]["rules"][0]["when"] = {"browser": "chrome"}
    assert "when.browser" in error_for(tmp_path, data)


def test_undocumented_takes_shape_keys_only(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["request_cost"]["undocumented"] = ["weather"]
    assert "request_cost.undocumented.0" in error_for(tmp_path, data)


def test_pay_as_you_go_needs_a_meter_in_currency(tmp_path):
    data = load("pay_as_you_go.yaml")
    data["billing"][0]["meter"]["unit"] = "credit"
    assert "needs meter.unit `usd`" in error_for(tmp_path, data)


def test_plan_units_cannot_be_metered_in_currency(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["meter"]["unit"] = "usd"
    assert "needs meter.unit `request` or `credit`" in error_for(tmp_path, data)


def test_the_benchmark_plan_must_exist(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["benchmark_account"]["plan"] = "enterprise"
    assert "is not one of the plans" in error_for(tmp_path, data)


def test_rate_card_is_checked_for_consistency(tmp_path):
    data = load("rate_card.yaml")
    data["billing"][0]["purchase"]["benchmarked_shape"] = "stealth"
    assert "is not under `shapes`" in error_for(tmp_path, data)
    data = load("rate_card.yaml")
    data["billing"][0]["purchase"]["plans"][0]["rates_per_1k"]["stealth"] = 3.0
    assert "not defined" in error_for(tmp_path, data)


def test_an_unknown_pricing_kind_is_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    data["billing"][0]["purchase"]["pricing"] = "auction"
    assert "billing.0.purchase" in error_for(tmp_path, data)


def test_duplicate_plan_ids_are_rejected(tmp_path):
    data = load("credit_subscription.yaml")
    plans = data["billing"][0]["purchase"]["plans"]
    plans.append(copy.deepcopy(plans[0]))
    assert "duplicate plan id" in error_for(tmp_path, data)


def test_unknown_and_na_are_values(tmp_path):
    data = load("credit_subscription.yaml")
    entry = data["billing"][0]
    entry["request_cost"]["base"] = "unknown"
    entry["charges"]["timeout"] = {"charged": "n/a"}
    entry["benchmark_account"]["plan"] = "unknown"
    config = ProviderConfig.model_validate(data)
    assert config.billing[0].request_cost.base == "unknown"
    assert config.billing[0].charges.timeout.charged == "n/a"


def test_json_schema_can_be_generated():
    schema = ProviderConfig.model_json_schema(by_alias=True)
    text = json.dumps(schema)
    assert schema["properties"]["billing"]["type"] == "array"
    assert '"set"' in text and "set_" not in text  # the alias, as written in YAML
    for kind in ("plan_units", "rate_card", "pay_as_you_go"):
        assert kind in text
