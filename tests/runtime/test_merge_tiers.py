"""Merge tiers 3a: the pure classifier and its git/config helpers."""
import subprocess
from types import SimpleNamespace

import pytest

from navigator.config import RepoConfig, Settings
from navigator.runtime.merge_tiers import (changed_files_for_run, classify, classify_for_repo,
                                           repo_config_for, tier_for_run)


def test_tests_and_docs_only_is_a():
    assert classify(["tests/test_x.py", "README.md", "docs/a.md"],
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


# --- 3a2: money, auth and key/cert/database paths fail closed to C -----------------------

@pytest.mark.parametrize("path", [
    "billing.py", "app/billing/plans.py", "payments/stripe.py", "app/payment_service.py", "app/pay.py",
    "orders.py", "app/orders/create.py", "wallet.py", "app/wallets.py", "ledger.py", "invoice.py",
    "stripe_client.py", "app/checkout.py", "app/paypal_hooks.py", "app/refunds.py", "app/auth/jwt.py",
    "x/authz/roles.py", "x/oauth/google.py", "session.py", "app/sessions.py", "permissions.py", "app/perms.py",
    "x.crt", "a.p12", "a.pfx", "a.jks", "keys/id_rsa", "keys/id_rsa.pub", "id_ed25519", "docs/API_KEYS.md",
    "config/api_key.txt", "apikey.txt",
    # agent-instruction files steer every later build and must never auto-merge as "docs"
    "CLAUDE.md", "docs/CLAUDE.md", "AGENTS.md", ".claude/settings.json",
    "my_private_key.txt", "data.db", "x.sqlite", "x.sqlite3",
])
def test_money_auth_key_paths_are_c(path):
    # NEGATIVE: on main every case is B (A for docs/API_KEYS.md, config/api_key.txt, apikey.txt,
    # my_private_key.txt): no pattern covered money, auth directories, certificates, key stores or databases
    assert classify([path], repo_name="waker") == "C"
    assert classify(["README.md", path], repo_name="waker") == "C"


def test_money_patterns_do_not_catch_common_code_names():
    # pins the pattern choice: not 'pay*' (payload.py), not '*order*' (recorder.py, border.css)
    assert classify(["app/payload.py", "app/recorder.py", "static/border.css"], repo_name="waker") == "B"


def test_component_match_is_c_only():
    # a component match reaches C (app/auth/ is an auth directory) ...
    assert classify(["app/auth/jwt.py"], repo_name="waker") == "C"
    # ... but a directory named like a doc never makes its contents a doc (would fail open to A)
    assert classify(["notes.md/build.go"], repo_name="waker") == "B"
    assert classify(["README.md/Dockerfile"], repo_name="waker") == "B"
    assert classify(["x.txt/Makefile"], repo_name="waker") == "B"


def test_conftest_is_code():
    # NEGATIVE: all three were A on main; conftest.py runs code at import under the verifier
    assert classify(["tests/conftest.py"], repo_name="waker") == "B"
    assert classify(["conftest.py"], repo_name="waker") == "B"
    assert classify(["backend/tests/conftest.py"], repo_name="waker") == "B"
    assert classify(["tests/fixtures/x.json", "tests/data/x.txt", "tests/test_x.py"],
                    repo_name="waker") == "A"


def test_test_named_files_outside_a_tests_dir_are_code():
    # NEGATIVE: all three were A on main (TEST_A matched test_*.py / *_test.py by basename anywhere)
    assert classify(["app/test_mode.py"], repo_name="waker") == "B"
    assert classify(["app/foo_test.py"], repo_name="waker") == "B"
    assert classify(["test_x.py"], repo_name="waker") == "B"
    assert classify(["tests/test_x.py", "backend/tests/test_y.py"],
                    repo_name="waker") == "A"


def test_per_repo_patterns_are_case_insensitive():
    # NEGATIVE: all three were B on main (the path is lowercased in _match, the pattern was not)
    assert classify(["App/Widgets.py"], repo_name="x", tier_c_paths=["App/Widg*"]) == "C"
    assert classify(["app/widgets.py"], repo_name="x", tier_c_paths=["App/Widg*"]) == "C"
    assert classify(["Backend/Core/x.py"], repo_name="x", tier_c_paths=["BACKEND/CORE/*"]) == "C"


def test_whitespace_padded_names_fail_closed():
    # a name whose stripped form differs from itself is nobody's honest change
    assert classify([" config.py"], repo_name="waker") == "C"
    assert classify(["docs/a.md "], repo_name="waker") == "C"


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


def test_changed_files_for_run_keeps_quote_and_backslash_names_unquoted(tmp_path):
    # NEGATIVE: on main `-c core.quotePath=false diff --name-only` still printed the first two names
    # C-quoted ('"docs/a\\"b.md"', '"back\\\\slash.md"'), so no pattern matched them and the tier was B
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "docs").mkdir()
    (repo / "docs" / 'a"b.md').write_text("# a\n")
    (repo / "back\\slash.md").write_text("# b\n")  # a single backslash in the name
    (repo / "na\u00efve.md").write_text("# c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs")
    verified = _git(repo, "rev-parse", "HEAD").strip()
    run = SimpleNamespace(base_commit_sha=base, verified_sha=verified)
    assert (sorted(changed_files_for_run(str(repo), run))
            == sorted(['docs/a"b.md', "back\\slash.md", "na\u00efve.md"]))
    assert tier_for_run(str(repo), run, None) == "A"


def test_repo_config_for_matches_resolved_paths(tmp_path):
    (tmp_path / "r").mkdir()
    settings = Settings(repos=[RepoConfig(path=str(tmp_path / "r"), tier_floor="C")])
    assert repo_config_for(settings, str(tmp_path / "r" / ".." / "r")).tier_floor == "C"
    assert repo_config_for(settings, str(tmp_path / "other")) is None
    assert repo_config_for(Settings(), str(tmp_path / "r")) is None
