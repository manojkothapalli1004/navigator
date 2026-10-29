"""Merge tiers 3a: the pure classifier and its git/config helpers."""
import subprocess
from types import SimpleNamespace

import pytest

from navigator.config import RepoConfig, Settings
from navigator.runtime.merge_tiers import (changed_files_for_run, classify, classify_for_repo,
                                           repo_config_for, tier_for_run)


def test_tests_and_docs_only_is_a():
    assert classify(["tests/test_x.py", "README.md", "docs/a.md", "backend/tests/conftest.py"],
                    repo_name="waker") == "A"


def test_code_is_b():
    assert classify(["backend/app/push.py"], repo_name="waker") == "B"
    assert classify(["vent/templates/home.html", "tests/test_x.py"], repo_name="vent") == "B"


@pytest.mark.parametrize("path", [
    ".env", "backend/.env.local", "certs/x.pem", "backend/app/deps.py", "backend/app/routes/pings.py",
    "backend/app/models.py", "vent/auth.py", "vent/legal.py", "vent/config.py", "alembic/versions/001.py",
    "pyproject.toml", "uv.lock", "requirements-dev.txt", "deploy/x.plist", "scripts/run.sh",
    "tests/test_tokenizer.py",  # token substring: fail closed
    "docs/SECRETS.md", "API_TOKEN.txt",  # upper case must still match *secret* / *token*
    "deploy/na\u00efve.sh",  # non-ASCII path (quoting is off in the git call)
])
def test_c_patterns(path):
    assert classify([path], repo_name="waker") == "C"
    assert classify(["README.md", path], repo_name="waker") == "C"


def test_docs_dir_code_is_not_a():
    # NEGATIVE: a doc directory does not make code a doc
    assert classify(["docs/conf.py"], repo_name="waker") == "B"
    assert classify(["docs/build.sh"], repo_name="waker") == "C"


def test_navigator_safety_paths_are_c_only_for_navigator():
    assert classify(["navigator/safety/command_runner.py"], repo_name="navigator") == "C"
    assert classify(["navigator/runtime/phases.py"], repo_name="navigator") == "C"
    assert classify(["navigator/observability/decisions.py"], repo_name="navigator") == "B"
    assert classify(["navigator/runtime/phases.py"], repo_name="other") == "B"


def test_trader_is_always_c():
    assert (classify(["README.md"], repo_name="trader") == "A") is False
    assert classify(["README.md"], repo_name="trader") == "C"


def test_empty_change_is_c():
    assert classify([], repo_name="waker") == "C"
    assert classify(None, repo_name="waker") == "C"


def test_floor_and_extra_paths():
    assert classify(["README.md"], repo_name="x", tier_floor="B") == "B"
    assert classify(["app/x.py"], repo_name="x", tier_floor="C") == "C"
    assert classify(["app/pay.py"], repo_name="x", tier_c_paths=["app/pay*"]) == "C"
    with pytest.raises(ValueError):
        classify(["README.md"], repo_name="x", tier_floor="D")


def test_classify_for_repo_uses_the_repo_config():
    cfg = RepoConfig(path="/r/x", tier_c_paths=["app/pay*"], tier_floor="B")
    assert classify_for_repo(["README.md"], "/r/x", cfg) == "B"
    assert classify_for_repo(["app/pay.py"], "/r/x", cfg) == "C"
    assert classify_for_repo(["README.md"], "/r/x", None) == "A"
    assert classify_for_repo(["README.md"], "/r/trader", None) == "C"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_tier_for_run_reads_the_runs_own_commits(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "nav/run-1")
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("# x\n")
    _git(repo, "add", "docs/x.md")
    _git(repo, "commit", "-m", "docs")
    verified = _git(repo, "rev-parse", "HEAD").strip()
    run = SimpleNamespace(base_commit_sha=base, verified_sha=verified)
    assert changed_files_for_run(str(repo), run) == ["docs/x.md"]
    assert tier_for_run(str(repo), run, None) == "A"
    assert tier_for_run(str(repo), SimpleNamespace(base_commit_sha=base, verified_sha=None), None) is None
    bogus = SimpleNamespace(base_commit_sha=base, verified_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert changed_files_for_run(str(repo), bogus) is None
    assert tier_for_run(str(repo), bogus, None) is None


def test_repo_config_for_matches_resolved_paths(tmp_path):
    (tmp_path / "r").mkdir()
    settings = Settings(repos=[RepoConfig(path=str(tmp_path / "r"), tier_floor="C")])
    assert repo_config_for(settings, str(tmp_path / "r" / ".." / "r")).tier_floor == "C"
    assert repo_config_for(settings, str(tmp_path / "other")) is None
    assert repo_config_for(Settings(), str(tmp_path / "r")) is None
