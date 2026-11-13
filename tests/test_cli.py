"""The command line, end to end, against the offline mock."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import shutil

import pytest
from conftest import ROOT

from serp_bench import cli
from serp_bench.config import config_hash, load_config
from serp_bench.metrics import to_markdown
from serp_bench.queries import per_run_counts
from serp_bench.records import read_records

CONFIG = ["--config", str(ROOT / "bench.yaml")]


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    """One mock run, shared by the tests that only read its output."""
    out = tmp_path_factory.mktemp("run")
    capture = pytest.MonkeyPatch()
    summary = out / "summary.md"
    capture.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = cli.main(
        ["run", *CONFIG, "--providers", "mock", "--run", "0", "--limit", "6",
         "--interval", "0.2", "--out", str(out), "--log-level", "WARNING"]
    )  # fmt: skip
    capture.undo()
    assert code == 0
    path = next(out.rglob("*.jsonl.gz"))
    return out, path, summary


def test_run_writes_records_in_the_dated_tree(finished_run):
    out, path, _ = finished_run
    today = dt.datetime.now(dt.UTC).date()
    assert path.parent == out / "raw" / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
    assert path.name.startswith("run-") and path.name.endswith(".jsonl.gz")
    records = list(read_records(path))
    assert len(records) == 7 and sum(r.warmup for r in records) == 1
    assert {r.provider for r in records} == {"mock"}
    assert {r.run_index for r in records} == {0}
    assert len({r.config_hash for r in records}) == 1


def test_the_job_summary_is_markdown(finished_run):
    _, _, summary = finished_run
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("## SERP benchmark run `")
    assert "| Metric | Value | How it is computed |" in text


def test_verify_recomputes_the_same_report(finished_run, capsys):
    _, path, _ = finished_run
    assert cli.main(["verify", str(path)]) == 0
    first = capsys.readouterr().out
    assert "7 records, schema valid" in first
    assert "mock (shadow, not published)" in first
    assert cli.main(["verify", str(path)]) == 0
    assert capsys.readouterr().out == first


def test_explain_prints_the_trail_of_one_request(finished_run, capsys):
    _, path, _ = finished_run
    record = next(r for r in read_records(path) if not r.warmup)
    assert cli.main(["explain", str(path), "--task", record.task_id]) == 0
    out = capsys.readouterr().out
    assert f"Task {record.task_id}, provider mock" in out
    assert "checks, in order:" in out and "dispatched" in out
    assert f"outcome: {record.outcome}" in out
    assert cli.main(["explain", str(path), "--task", "nope"]) == 2


def test_verify_rejects_a_damaged_file(tmp_path, finished_run, capsys):
    _, path, _ = finished_run
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    broken = json.loads(lines[1])
    broken["outcome"] = "excellent"
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join([lines[0], json.dumps(broken)]) + "\n", encoding="utf-8")
    assert cli.main(["verify", str(bad)]) == 2
    assert "bad.jsonl:2: unknown outcome 'excellent'" in capsys.readouterr().err


def test_run_logs_a_header_and_one_line_per_request(tmp_path, capsys):
    code = cli.main(
        ["run", *CONFIG, "--providers", "mock", "--run", "1", "--limit", "3",
         "--interval", "0.2", "--out", str(tmp_path)]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0
    log = captured.err
    assert "methodology 0.1, configuration" in log
    assert "provider mock: status shadow, plan mock" in log
    assert "one task every 0.2 s to all providers at once" in log and "no retries" in log
    assert log.count("| mock |") == 4  # the warm-up and three tasks
    assert "Recompute this report with: serp-bench verify" in captured.out


def test_json_logs_are_one_object_per_line_with_the_report(tmp_path, capsys):
    code = cli.main(
        ["run", *CONFIG, "--providers", "mock", "--run", "2", "--limit", "2", "--interval", "0.2",
         "--out", str(tmp_path), "--log-format", "json"]
    )  # fmt: skip
    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    names = [event["event"] for event in events]
    assert names[0] == "run_start" and names[-1] == "report"
    assert names.count("request") == 3
    request = next(e for e in events if e["event"] == "request" and not e["warmup"])
    assert {"task_id", "provider", "outcome", "total_ms", "dispatch_lag_ms"} <= set(request)
    report = events[-1]["report"]
    # two tasks may both come from pools outside the headline, so only the shape is fixed
    assert report["by_pool"]
    assert {row["provider"] for row in report["headline"]} <= {"mock"}


def test_a_draft_provider_is_refused_unless_allowed(tmp_path, capsys, monkeypatch):
    # A copy of the real folder, put back to `draft`: the test must not depend on where the
    # real provider is in its onboarding, and must never reach the network.
    providers = tmp_path / "providers"
    shutil.copytree(ROOT / "providers" / "scrapebeaver", providers / "scrapebeaver")
    config = providers / "scrapebeaver" / "provider.yaml"
    text = re.sub(r"(?m)^status: \w+", "status: draft", config.read_text(encoding="utf-8"))
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SCRAPEBEAVER_API_KEY", "k")
    code = cli.main(
        [
            "run",
            *CONFIG,
            "--providers-dir",
            str(providers),
            "--providers",
            "scrapebeaver",
            "--run",
            "0",
            "--limit",
            "1",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert "status `draft`" in capsys.readouterr().err
    assert not list(tmp_path.rglob("*.jsonl.gz"))


def test_a_provider_without_its_key_is_skipped_with_a_message(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SCRAPEBEAVER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda path: None)
    code = cli.main(
        ["run", *CONFIG, "--run", "0", "--limit", "1", "--allow-draft", "--out", str(tmp_path)]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "skipping scrapebeaver: SCRAPEBEAVER_API_KEY is not set" in captured.err
    assert "no provider can run" in captured.err


def test_auto_exits_cleanly_when_no_run_is_due(tmp_path, capsys, monkeypatch):
    class Quiet(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            from serp_bench.config import load_config

            schedule = load_config(ROOT / "bench.yaml").schedule
            day = dt.date(2026, 11, 12)
            hour = (schedule.due_hour(day, 0) + 1) % 24
            return cls(2026, 11, 12, hour, 30, tzinfo=dt.UTC)

    monkeypatch.setattr(cli.dt, "datetime", Quiet)
    code = cli.main(["run", *CONFIG, "--providers", "mock", "--out", str(tmp_path)])
    assert code == 0
    assert "Nothing due" in capsys.readouterr().out
    assert not list(tmp_path.rglob("*.jsonl.gz"))


def test_unknown_provider_and_bad_run_are_readable_errors(tmp_path, capsys):
    assert cli.main(["run", *CONFIG, "--providers", "nobody", "--run", "0"]) == 2
    assert "unknown provider(s) ['nobody']" in capsys.readouterr().err
    assert cli.main(["run", *CONFIG, "--providers", "mock", "--run", "9"]) == 2
    assert "must be `auto` or a run index from 0 to 3" in capsys.readouterr().err


def test_plan_prints_the_arithmetic(capsys):
    assert cli.main(["plan", *CONFIG, "--date", "2026-11-12", "--run", "1", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "run 0 at" in out and "moves one hour later each day" in out
    # the numbers come from bench.yaml, so the test follows it when the volume changes
    config = load_config(ROOT / "bench.yaml")
    runs = config.schedule.runs_per_day
    core = per_run_counts(config.pools["core"].per_day, runs)
    assert ", ".join(f"run {i}: {n}" for i, n in enumerate(core)) in out
    in_run = sum(per_run_counts(p.per_day, runs)[1] for p in config.pools.values())
    per_day = sum(p.per_day for p in config.pools.values())
    assert f"{in_run} tasks in this run, {per_day} per day per provider" in out
    assert f"... and {in_run - 2} more" in out


def test_providers_lists_status_and_key(capsys, monkeypatch):
    monkeypatch.delenv("SCRAPEBEAVER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda path: None)
    assert cli.main(["providers", *CONFIG]) == 0
    out = capsys.readouterr().out
    assert "mock" in out and "not needed" in out
    assert "scrapebeaver" in out and "missing" in out


def test_price_prints_a_quote_with_its_formula(capsys):
    args = ["price", *CONFIG, "--provider", "scrapebeaver", "--date", "2026-11-12"]
    assert cli.main([*args, "--volume", "50000"]) == 0
    out = capsys.readouterr().out
    assert "Price per 1,000 at 50,000 a month: $0.90" in out
    assert "Starter: $25.00 for 25,000 searches + 25,000 over the plan" in out
    assert cli.main([*args, "--volume", "50000", "--plan-only"]) == 0
    out = capsys.readouterr().out
    assert "Price per 1,000 at 50,000 a month: $1.60" in out
    assert "Growth: $80.00 for 100,000 searches" in out and "over the plan" not in out
    assert "Declared by the provider" in out
    assert cli.main([*args, "--volume", "100000", "--date", "2020-01-01"]) == 0
    assert "unknown (no billing entry covers 2020-01-01" in capsys.readouterr().out


def test_schema_is_json(capsys):
    assert cli.main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "ProviderConfig"
    assert set(schema["properties"]["status"]["enum"]) == {
        "draft",
        "conformance",
        "shadow",
        "published",
    }


def test_metrics_prints_the_registry(capsys):
    assert cli.main(["metrics"]) == 0
    assert capsys.readouterr().out.strip() == to_markdown()


def test_the_readme_section_is_the_registry():
    """The README cannot drift: its section is generated, and this checks it is current.
    Refresh it with `serp-bench metrics --update-readme README.md`."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert cli.readme_block() in readme


def test_update_readme_rewrites_only_its_section(tmp_path):
    path = tmp_path / "README.md"
    path.write_text(f"before\n{cli.README_START}\nold\n{cli.README_END}\nafter\n", encoding="utf-8")
    assert cli.main(["metrics", "--update-readme", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    assert text.startswith("before\n") and text.endswith("\nafter\n")
    assert cli.readme_block() in text and "\nold\n" not in text


def test_conformance_of_the_mock_passes_offline(tmp_path, capsys):
    code = cli.main(
        ["conformance", *CONFIG, "--provider", "mock", "--out", str(tmp_path),
         "--log-level", "WARNING"]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "Conformance of mock" in out and out.splitlines()[0].endswith(": PASS")
    data = json.loads((tmp_path / "conformance.json").read_text(encoding="utf-8"))
    assert data["overall"] == "PASS"


def test_conformance_needs_the_key(capsys, monkeypatch):
    monkeypatch.delenv("SCRAPEBEAVER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda path: None)
    assert cli.main(["conformance", *CONFIG, "--provider", "scrapebeaver"]) == 2
    assert "SCRAPEBEAVER_API_KEY is not set" in capsys.readouterr().err


def test_check_providers_passes_on_the_shipped_tree(capsys):
    assert cli.main(["check-providers", *CONFIG]) == 0
    out = capsys.readouterr().out
    # Whatever number of providers the tree holds, none of them has an error.
    assert re.search(r"\d+ provider folder\(s\) checked, 0 error\(s\)\.", out)
    assert "FAIL" not in out


def test_check_providers_reports_problems_and_stale_conformance(tmp_path, capsys, monkeypatch):
    providers = tmp_path / "providers"
    shutil.copytree(ROOT / "providers" / "scrapebeaver", providers / "scrapebeaver")
    config = providers / "scrapebeaver" / "provider.yaml"
    text = re.sub(r"(?m)^status: \w+", "status: shadow", config.read_text(encoding="utf-8"))
    config.write_text(text, encoding="utf-8")
    (providers / "scrapebeaver" / "conformance.json").unlink(missing_ok=True)  # start without one
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    args = ["check-providers", "--providers-dir", str(providers)]

    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert (
        "warning: no conformance.json" in out and "::warning title=provider scrapebeaver::" in out
    )

    (providers / "scrapebeaver" / "conformance.json").write_text('{"date": "2026-01-01"}', "utf-8")
    assert cli.main(args) == 0
    assert "days old (limit 45)" in capsys.readouterr().out

    fixture = providers / "scrapebeaver" / "fixtures" / "success.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["expect"]["organic_count"] = 99
    fixture.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "fixtures/success.json: organic_count is 3, expected 99" in out
    assert "::error title=provider scrapebeaver::" in out


def test_no_secret_reaches_logs_or_records(tmp_path, capsys, monkeypatch):
    """A real adapter, a key, a failing endpoint: the key must appear nowhere."""
    secret = "psk_live_do_not_leak_0123456789"
    monkeypatch.setenv("SCRAPEBEAVER_API_KEY", secret)
    monkeypatch.setenv("SCRAPEBEAVER_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    code = cli.main(
        ["run", *CONFIG, "--providers", "scrapebeaver", "--allow-draft", "--run", "0",
         "--limit", "2", "--interval", "0.05", "--out", str(tmp_path), "--log-level", "DEBUG"]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0
    path = next(tmp_path.rglob("*.jsonl.gz"))
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        stored = handle.read()
    assert secret not in captured.err and secret not in captured.out and secret not in stored
    assert {r.outcome for r in read_records(path)} == {"network"}


# --- budget -------------------------------------------------------------------------------


def test_budget_prices_a_month_of_the_configuration(capsys):
    assert cli.main(["budget", *CONFIG, "--providers", "scrapebeaver,mock"]) == 0
    out = capsys.readouterr().out
    config = load_config(ROOT / "bench.yaml")
    per_day = sum(pool.per_day for pool in config.pools.values())
    controls = config.limits.max_control_repeats_per_provider_per_run
    per_run_extra = 1 + controls  # the warm-up, and at most this many control repeats
    month = (per_day + config.schedule.runs_per_day * per_run_extra) * cli.DAYS_IN_A_MONTH
    assert f"{month:,}" in out and "Ceiling: $50.00" in out
    assert "pool core:" in out and "warm-ups:" in out and "control repeats (at most):" in out
    assert "OVER BUDGET" not in out and "searches" in out


def test_budget_fails_when_the_cheapest_covering_plan_is_above_the_ceiling(tmp_path, capsys):
    """Enough volume to outgrow ScrapeBeaver's $25 plan: the $80 one breaks a $50 ceiling."""
    shutil.copytree(ROOT / "queries", tmp_path / "queries")
    text = (ROOT / "bench.yaml").read_text(encoding="utf-8")
    text = text.replace("per_day: 40 ", "per_day: 900 ")
    assert "per_day: 900" in text
    (tmp_path / "bench.yaml").write_text(text, encoding="utf-8")
    args = ["budget", "--config", str(tmp_path / "bench.yaml")]
    args += ["--providers-dir", str(ROOT / "providers"), "--providers", "scrapebeaver"]
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "OVER BUDGET: ScrapeBeaver" in out and "Growth" in out and "$80.00" in out


def test_the_budget_ceiling_does_not_change_the_configuration_hash(tmp_path):
    shutil.copytree(ROOT / "queries", tmp_path / "queries")
    text = (ROOT / "bench.yaml").read_text(encoding="utf-8")
    (tmp_path / "bench.yaml").write_text(text, encoding="utf-8")
    before = config_hash(load_config(tmp_path / "bench.yaml"), tmp_path)
    changed = text.replace(
        "max_usd_per_provider_per_month: 50", "max_usd_per_provider_per_month: 75"
    )
    assert changed != text
    (tmp_path / "bench.yaml").write_text(changed, encoding="utf-8")
    assert config_hash(load_config(tmp_path / "bench.yaml"), tmp_path) == before


def test_verify_refuses_a_file_that_was_cut_short(tmp_path, capsys):
    """A run that was killed leaves a gzip stream with no end. That is not a finished run:
    `verify` must say so and fail, so that CI does not store it."""
    assert cli.main(["run", *CONFIG, "--providers", "mock", "--run", "0", "--limit", "4",
                     "--interval", "0.01", "--out", str(tmp_path)]) == 0  # fmt: skip
    capsys.readouterr()
    path = next(tmp_path.rglob("run-*.jsonl.gz"))
    path.write_bytes(path.read_bytes()[:-20])
    assert cli.main(["verify", str(path)]) == 2
    assert "incomplete" in capsys.readouterr().err
