"""Publishing the summary on its own branch: one file, one commit, replaced every time.

Every repository here is a throwaway one on disk; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

SCRIPT = ROOT / "scripts" / "store_summary.sh"
BRANCH = "benchmark-summary"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    repo = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", str(repo))
    return repo


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """Where the script runs: any repository, with a git identity that must not be used."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.name", "A Person")
    git(repo, "config", "user.email", "person@example.com")
    return repo


def summary(work: Path, **fields: object) -> Path:
    path = work / "summary.json"
    body = {"generated_at": "2026-11-15T12:00:00Z", "providers": [], **fields}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def store(work: Path, remote: Path, file: Path) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [str(SCRIPT), str(file), BRANCH, str(remote)],
        cwd=work,
        capture_output=True,
        text=True,
        env=clean,
        check=False,
    )


def test_the_branch_holds_the_summary_and_a_readme(work, remote):
    done = store(work, remote, summary(work, marker="first"))
    assert done.returncode == 0, done.stderr
    assert git(remote, "ls-tree", "--name-only", BRANCH).split() == ["README.md", "summary.json"]
    assert json.loads(git(remote, "show", f"{BRANCH}:summary.json"))["marker"] == "first"


def test_a_new_summary_replaces_the_old_one_and_leaves_no_history(work, remote):
    store(work, remote, summary(work, marker="first"))
    done = store(work, remote, summary(work, marker="second"))
    assert done.returncode == 0, done.stderr
    assert json.loads(git(remote, "show", f"{BRANCH}:summary.json"))["marker"] == "second"
    assert git(remote, "rev-list", "--count", BRANCH) == "1"


def test_the_commit_is_made_as_the_bot_not_as_the_person_at_the_machine(work, remote):
    store(work, remote, summary(work))
    who = git(remote, "log", "-1", "--format=%an|%ae|%cn|%ce", BRANCH).split("|")
    assert who[0] == who[2] == "github-actions[bot]"
    assert who[1] == who[3] and who[1].endswith("@users.noreply.github.com")
    assert "person" not in " ".join(who).lower()


def test_the_commit_does_not_start_builds(work, remote):
    store(work, remote, summary(work))
    assert "[skip ci]" in git(remote, "log", "-1", "--format=%s", BRANCH)


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"providers": "none"}'])
def test_a_file_that_is_not_a_summary_is_refused_and_the_branch_is_kept(work, remote, content):
    store(work, remote, summary(work, marker="good"))
    bad = work / "bad.json"
    bad.write_text(content, encoding="utf-8")
    done = store(work, remote, bad)
    assert done.returncode != 0
    assert json.loads(git(remote, "show", f"{BRANCH}:summary.json"))["marker"] == "good"


def test_the_working_tree_is_left_alone(work, remote):
    file = summary(work)
    store(work, remote, file)
    assert git(work, "status", "--porcelain").split() == ["??", "summary.json"]
    assert git(work, "branch", "--list") == ""  # no local branch was made
