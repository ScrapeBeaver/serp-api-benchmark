"""Every provider folder is held to the same contract. Nothing here names a provider:
adding a folder adds its tests."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import PROVIDERS_DIR, ROOT, make_task

from serp_bench.fixtures import (
    DUMMY_SECRET,
    check_fixture,
    dummy_env,
    fixtures_of,
    load_fixture,
)
from serp_bench.models import Device, RawResponse
from serp_bench.provider import (
    Provider,
    ProviderConfigError,
    discover,
    load_adapter_class,
    load_spec,
)
from serp_bench.validate import void_reason

SPECS = discover(PROVIDERS_DIR)
FIXTURES = [(spec, path) for spec in SPECS.values() for path in fixtures_of(spec)]


def test_discovery_finds_provider_folders_and_skips_the_template():
    assert {"mock", "scrapebeaver"} <= set(SPECS)
    assert not any(provider_id.startswith("_") for provider_id in SPECS)
    for spec in SPECS.values():
        assert issubclass(spec.adapter_class, Provider)
        assert spec.config.id == spec.folder.name


def test_the_template_is_a_valid_provider_folder():
    spec = load_spec(PROVIDERS_DIR / "_template")
    provider = spec.create(dummy_env(spec))
    request = provider.build_request(make_task())
    assert request.url.startswith("https://")


def test_a_new_folder_is_picked_up_without_touching_the_package(tmp_path):
    target = tmp_path / "providers" / "newcomer"
    shutil.copytree(PROVIDERS_DIR / "_template", target)
    config = target / "provider.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("id: example ", "id: newcomer "), "utf-8"
    )
    specs = discover(tmp_path / "providers")
    assert list(specs) == ["newcomer"]
    assert specs["newcomer"].create({"EXAMPLE_API_KEY": "k"}).id == "newcomer"


def test_folder_and_id_must_match(tmp_path):
    target = tmp_path / "providers" / "other"
    shutil.copytree(PROVIDERS_DIR / "mock", target)
    with pytest.raises(ProviderConfigError, match="must match"):
        discover(tmp_path / "providers")


def test_an_adapter_must_define_exactly_one_provider(tmp_path):
    target = tmp_path / "providers" / "mock"
    shutil.copytree(PROVIDERS_DIR / "mock", target)
    adapter = target / "adapter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8") + "\n\nclass Second(Mock):\n    pass\n", "utf-8"
    )
    with pytest.raises(ProviderConfigError, match="exactly one Provider subclass, found 2"):
        discover(tmp_path / "providers")


@pytest.mark.parametrize("spec", SPECS.values(), ids=list(SPECS))
def test_a_provider_with_a_key_ships_fixtures(spec):
    if not spec.config.auth.env:
        pytest.skip("offline provider")
    paths = fixtures_of(spec)
    assert paths, "at least one fixture"
    outcomes = {load_fixture(path).expect["outcome"] for path in paths}
    assert "delivered" in outcomes, "one fixture must be a delivered answer"
    assert len(outcomes) > 1, "and one must be something else"


@pytest.mark.parametrize(
    ("spec", "path"), FIXTURES, ids=[f"{s.config.id}/{p.name}" for s, p in FIXTURES]
)
def test_the_adapter_reads_its_fixture_as_expected(spec, path):
    fixture = load_fixture(path)
    assert fixture.source, "a fixture says where it comes from"
    assert check_fixture(spec.create(dummy_env(spec)), fixture) == []


@pytest.mark.parametrize("spec", SPECS.values(), ids=list(SPECS))
def test_requests_can_be_built_for_every_declared_capability(spec):
    provider = spec.create(dummy_env(spec))
    caps = spec.config.capabilities
    tasks = [make_task()]
    if caps.mobile is True:
        tasks.append(make_task(device=Device.mobile))
    if caps.pagination is True:
        tasks.append(make_task(page=2))
    if caps.city_location is True:
        tasks.append(make_task(location="Austin,Texas,United States"))
    for task in tasks:
        request = provider.build_request(task)
        assert request.method in ("GET", "POST")
        assert request.url.startswith(("https://", "http://"))


@pytest.mark.parametrize("spec", SPECS.values(), ids=list(SPECS))
def test_no_secret_survives_into_the_loggable_request(spec):
    provider = spec.create(dummy_env(spec))
    request = provider.build_request(make_task())
    sent = json.dumps(request.model_dump(), default=str)
    shown = json.dumps(provider.loggable(request), default=str)
    assert DUMMY_SECRET not in shown
    if spec.config.auth.env:
        assert DUMMY_SECRET in sent, "the test must be able to see the secret it guards"
        assert "***" in shown


@pytest.mark.parametrize("spec", SPECS.values(), ids=list(SPECS))
def test_a_missing_key_is_reported_not_guessed(spec):
    provider = spec.create({})
    assert provider.missing_env == spec.config.auth.env


def test_a_secret_that_was_not_declared_cannot_be_read(specs):
    provider = specs["scrapebeaver"].create({"SCRAPEBEAVER_API_KEY": "k", "OTHER": "x"})
    with pytest.raises(KeyError, match="not declared"):
        provider.secret("OTHER")


def test_the_base_url_can_be_overridden_from_the_environment(specs):
    env = {"SCRAPEBEAVER_API_KEY": "k", "SCRAPEBEAVER_BASE_URL": "http://127.0.0.1:9/"}
    provider = specs["scrapebeaver"].create(env)
    assert provider.build_request(make_task()).url == "http://127.0.0.1:9/v1/search/light"


def test_scrapebeaver_request_shape(specs):
    provider = specs["scrapebeaver"].create({"SCRAPEBEAVER_API_KEY": "k"})
    body = provider.build_request(make_task(country="gb", locale="en-GB")).json_body
    assert body == {
        "query": "coffee shops london",
        "country": "GB",
        "locale": "en-GB",
        "device": "desktop",
    }
    paged = provider.build_request(make_task(page=3, device=Device.mobile)).json_body
    assert (paged["start"], paged["device"]) == (20, "mobile")
    placed = provider.build_request(make_task(location="Austin,Texas,United States")).json_body
    # The example in the provider's documentation.
    assert placed["uule"] == "w+CAIQICIaQXVzdGluLFRleGFzLFVuaXRlZCBTdGF0ZXM"


def test_the_shipped_tree_has_no_stray_provider_files():
    for folder in PROVIDERS_DIR.iterdir():
        if folder.name == "_examples":  # contracts only: one <id>.yaml each, no adapter
            assert all(path.suffix == ".yaml" for path in folder.iterdir()), folder
            continue
        if folder.is_dir() and not folder.name.startswith("."):
            assert (folder / "provider.yaml").is_file(), folder
            assert (folder / "adapter.py").is_file(), folder
    assert (ROOT / "providers" / "README.md").is_file()


# --- an adapter is a translator: it measures nothing ----------------------------------------

ADAPTER_METHODS = {"build_request", "parse", "classify_error", "own_fault", "billed_units"}
MEASURING_MODULES = ("runner", "validate", "stats", "report", "metrics", "pricing", "records")


def _adapter_folders():
    return [
        folder
        for folder in sorted(PROVIDERS_DIR.iterdir())
        if folder.is_dir() and (folder / "adapter.py").is_file()
    ]


@pytest.mark.parametrize("folder", _adapter_folders(), ids=lambda folder: folder.name)
def test_an_adapter_overrides_only_the_translation_methods(folder):
    """Every number is computed by shared code. An adapter that overrode `supports`,
    `redact` or anything else of the base class could tilt the result for one provider."""
    cls = load_adapter_class(folder, folder.name.lstrip("_"))
    own = {
        name
        for name, value in vars(cls).items()
        if callable(value) and not name.startswith("_") and hasattr(Provider, name)
    }
    allowed = ADAPTER_METHODS | ({"transport"} if folder.name == "mock" else set())
    assert own <= allowed, f"{folder.name} overrides {sorted(own - allowed)}"


@pytest.mark.parametrize("folder", _adapter_folders(), ids=lambda folder: folder.name)
def test_an_adapter_does_not_import_the_code_that_measures(folder):
    source = (folder / "adapter.py").read_text(encoding="utf-8")
    for module in MEASURING_MODULES:
        assert f"serp_bench.{module}" not in source, f"{folder.name} imports serp_bench.{module}"
        assert f"from serp_bench import {module}" not in source
    for clock in ("import time", "from time", "perf_counter", "monotonic"):
        assert clock not in source, f"{folder.name} reads a clock: {clock}"


def test_an_adapter_can_void_a_request_only_for_our_own_key_or_credit(specs):
    class Generous(type(specs["mock"].create({}))):
        def own_fault(self, raw):
            return "provider_was_having_a_bad_day"

    provider = Generous(specs["mock"].config, {})
    raw = RawResponse(status=500, headers={}, body=b"{}", ttfb_ms=1.0, total_ms=1.0)
    assert void_reason(provider, raw) is None


def test_decodo_builds_the_documented_request(specs):
    provider = specs["decodo"].create({"DECODO_API_KEY": "secret-value"})
    request = provider.build_request(make_task(country="gb", locale="en-GB"))
    assert (request.method, request.url) == ("POST", "https://data.decodo.com/v1/search")
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert request.json_body == {"query": "coffee shops london", "geo": "GB", "locale": "en-GB"}
    paged = provider.build_request(make_task(page=3)).json_body
    assert paged["page"] == 3
    placed = provider.build_request(make_task(location="Austin,Texas,United States")).json_body
    assert placed["uule"] == "w+CAIQICIaQXVzdGluLFRleGFzLFVuaXRlZCBTdGF0ZXM"
    # The endpoint has no device parameter: a phone search is not sent to it at all.
    assert not provider.supports(make_task(device=Device.mobile))
    assert "secret-value" not in str(provider.loggable(request))


def test_a_recorded_decodo_page_has_one_unresolved_link(specs):
    """The live response of 2026-11-12: nine results, of which the fifth is Google's own
    /goto redirect. The page is delivered (step 1) and not resolved (step 2)."""
    from serp_bench.fixtures import load_fixture
    from serp_bench.validate import result_refs

    fixture = load_fixture(PROVIDERS_DIR / "decodo" / "fixtures" / "success.json")
    provider = specs["decodo"].create({"DECODO_API_KEY": "k"})
    refs = result_refs(provider.parse(fixture.raw))
    assert len(refs) == 9
    assert [ref.pos for ref in refs if ref.wrapper] == [5]
    assert all(ref.well_formed for ref in refs)


def test_scrapingbee_builds_the_documented_request(specs):
    provider = specs["scrapingbee"].create({"SCRAPINGBEE_API_KEY": "secret-value"})
    request = provider.build_request(make_task(country="GB", locale="en-GB"))
    assert (request.method, request.url) == ("GET", "https://app.scrapingbee.com/api/v1/google")
    # The key travels in a header, never in the address, where it could end up in a log.
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert "secret-value" not in request.url and "api_key" not in request.params
    assert request.params == {
        "search": "coffee shops london",
        "country_code": "gb",
        "device": "desktop",
        "extra_params": "locale=en-GB",
    }
    paged = provider.build_request(make_task(page=2, device=Device.mobile)).params
    assert (paged["page"], paged["device"]) == ("2", "mobile")
    # A place is offered only as coordinates, so a task that names a place is not sent.
    assert not provider.supports(make_task(location="Austin,Texas,United States"))
    assert "secret-value" not in str(provider.loggable(request))


def test_scrapingbee_reads_the_charge_and_the_query_it_used(specs):
    from serp_bench.fixtures import load_fixture

    fixture = load_fixture(PROVIDERS_DIR / "scrapingbee" / "fixtures" / "success.json")
    provider = specs["scrapingbee"].create({"SCRAPINGBEE_API_KEY": "k"})
    assert provider.billed_units(fixture.raw) == 10.0
    echo = provider.parse(fixture.raw).echo
    # The response names neither; both are read from the Google address it reports.
    assert (echo.query, echo.country, echo.locale) == ("best running shoes", "us", "en")


def test_scrapingbee_passes_on_the_request_id_and_the_substitution_flag(specs):
    from serp_bench.fixtures import load_fixture

    provider = specs["scrapingbee"].create({"SCRAPINGBEE_API_KEY": "k"})
    folder = PROVIDERS_DIR / "scrapingbee" / "fixtures"
    ordinary = provider.parse(load_fixture(folder / "success.json").raw)
    assert ordinary.provider_request_id == "00000000-0000-4000-8000-000000000001"
    assert ordinary.substituted is False
    # Google found nothing for the phrase and showed something else: the provider says so.
    assert provider.parse(load_fixture(folder / "substituted_results.json").raw).substituted is True


def test_scrapingdog_builds_the_documented_request(specs):
    provider = specs["scrapingdog"].create({"SCRAPINGDOG_API_KEY": "secret-value"})
    request = provider.build_request(make_task(country="GB", locale="en-GB"))
    assert (request.method, request.url) == ("GET", "https://api.scrapingdog.com/google")
    assert request.params == {
        "api_key": "secret-value",
        "query": "coffee shops london",
        "country": "gb",
        "language": "en",
        "page": "0",  # the first page is 0
        "advance_search": "false",
    }
    paged = provider.build_request(make_task(page=3, device=Device.mobile)).params
    assert (paged["page"], paged["mob_search"]) == ("2", "true")
    placed = provider.build_request(make_task(location="Austin,Texas,United States")).params
    assert placed["uule"] == "w+CAIQICIaQXVzdGluLFRleGFzLFVuaXRlZCBTdGF0ZXM"
    # The key has to travel in the address here, so what is logged must not carry it.
    assert "secret-value" not in str(provider.loggable(request))


def test_scrapingdog_reads_googles_note_about_the_result_set(specs):
    from serp_bench.fixtures import load_fixture

    provider = specs["scrapingdog"].create({"SCRAPINGDOG_API_KEY": "k"})
    folder = PROVIDERS_DIR / "scrapingdog" / "fixtures"
    ordinary = provider.parse(load_fixture(folder / "success.json").raw)
    assert ordinary.substituted is False
    # The live response reports no Google address, so it does not say which query it used.
    assert ordinary.echo.query is None
    assert provider.parse(load_fixture(folder / "substituted_results.json").raw).substituted is True


def test_a_recorded_scrapingdog_page_has_one_unresolved_link(specs):
    """The live response of 2026-11-12: nine results, of which the second is Google's own
    /url redirect. The page is delivered (step 1) and not resolved (step 2)."""
    from serp_bench.fixtures import load_fixture
    from serp_bench.validate import result_refs

    fixture = load_fixture(PROVIDERS_DIR / "scrapingdog" / "fixtures" / "success.json")
    provider = specs["scrapingdog"].create({"SCRAPINGDOG_API_KEY": "k"})
    refs = result_refs(provider.parse(fixture.raw))
    assert len(refs) == 9
    assert [ref.pos for ref in refs if ref.wrapper] == [2]


def test_brightdata_builds_the_documented_request(specs):
    env = {"BRIGHTDATA_API_KEY": "secret-value", "BRIGHTDATA_ZONE": "zone-name"}
    provider = specs["brightdata"].create(env)
    request = provider.build_request(make_task(country="GB", locale="en-GB"))
    assert (request.method, request.url) == ("POST", "https://api.brightdata.com/request")
    assert request.headers["Authorization"] == "Bearer secret-value"
    # The search itself is a Google address, with the query first and parsed JSON asked for.
    assert request.json_body == {
        "zone": "zone-name",
        "url": "https://www.google.com/search?q=coffee+shops+london&gl=gb&hl=en&brd_json=1",
        "format": "raw",
    }
    phone = provider.build_request(make_task(device=Device.mobile)).json_body["url"]
    assert phone.endswith("&brd_json=1&brd_mobile=1")
    third = provider.build_request(make_task(page=3)).json_body["url"]
    assert third.endswith("&start=20")
    placed = provider.build_request(make_task(location="Austin,Texas,United States"))
    assert placed.json_body["url"].endswith("&uule=Austin%2CTexas%2CUnited+States")
    # Neither the key nor the zone of the account can end up in a log.
    logged = str(provider.loggable(request))
    assert "secret-value" not in logged and "zone-name" not in logged


def test_brightdata_reads_what_was_billed_from_the_debug_header(specs):
    env = {"BRIGHTDATA_API_KEY": "k", "BRIGHTDATA_ZONE": "z"}
    provider = specs["brightdata"].create(env)

    def answer(**headers):
        return RawResponse(status=200, headers=headers, body=b"{}", ttfb_ms=1.0, total_ms=1.0)

    assert provider.billed_units(answer(**{"X-Brd-Debug": "req_id=1; billed=true"})) == 1.0
    assert provider.billed_units(answer(**{"x-brd-debug": "billed=false"})) == 0.0
    assert provider.billed_units(answer()) is None


def test_serper_builds_its_request(specs):
    provider = specs["serper"].create({"SERPER_API_KEY": "secret-value"})
    request = provider.build_request(make_task(country="GB", locale="en-GB"))
    assert (request.method, request.url) == ("POST", "https://google.serper.dev/search")
    # The key travels in a header, never in the address or the body.
    assert request.headers["X-API-KEY"] == "secret-value"
    assert request.json_body == {"q": "coffee shops london", "gl": "gb", "hl": "en"}
    assert provider.build_request(make_task(page=3)).json_body["page"] == 3
    placed = provider.build_request(make_task(location="Austin,Texas,United States")).json_body
    assert placed["location"] == "Austin,Texas,United States"
    assert "secret-value" not in str(provider.loggable(request))


def test_oxylabs_builds_the_documented_request(specs):
    provider = specs["oxylabs"].create({"OXYLABS_API_KEY": "secret-value"})
    request = provider.build_request(make_task(country="GB", locale="en-GB"))
    assert (request.method, request.url) == (
        "POST",
        "https://webapi.oxylabs.io/v1/scrape/google/search",
    )
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert request.json_body == {
        "query": "coffee shops london",
        "location": "GB",
        "locale": "en_GB",
        "output": ["json"],
    }
    assert provider.build_request(make_task(device=Device.mobile)).json_body["device"] == "mobile"
    assert provider.build_request(make_task(page=3)).json_body["start_page"] == 3
    # The endpoint takes a country only: a search for a city is not sent to it at all.
    assert not provider.supports(make_task(location="Austin,Texas,United States"))
    assert "secret-value" not in str(provider.loggable(request))
