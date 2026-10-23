import io
import json
import os
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from navigator import cli
from navigator.cli import run_cli
from navigator.clients.act_client import FakeACTClient
from navigator.config import RepoConfig, Settings, effective_repos
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, ProposalStatus
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run

SECRET_KEY = "sk-ant-supersecret-value-123"
SECRET_AUTH = "auth-hidden-value-456"


@pytest.fixture(autouse=True)
def _sdk_ok(monkeypatch):
    monkeypatch.setattr(cli, "_sdk_version", lambda: "0.2.165")


def _git_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _plist(tmp_path, env, top=None):
    p = tmp_path / "nav.plist"
    data = top if top is not None else {"Label": "com.calwbot.navigator",
                                        "EnvironmentVariables": env}
    with open(p, "wb") as f:
        plistlib.dump(data, f)
    return p


def _repos_file(tmp_path, content, name="repos.json"):
    f = tmp_path / name
    f.write_text(json.dumps(content))
    return f


def _doctor(plist):
    out = io.StringIO()
    rc = run_cli(["doctor", "--plist", str(plist)], act=None, out=out)
    return rc, out.getvalue()


def test_valid_list_of_objects_passes(tmp_path):
    repo = _git_repo(tmp_path)
    rf = _repos_file(tmp_path, [{"path": str(repo)}])
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(rf)}))
    assert rc == 0, text
    assert str(repo) in text


def test_list_of_strings_fails(tmp_path):
    repo = _git_repo(tmp_path)
    rf = _repos_file(tmp_path, [str(repo)])
    rc, _ = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(rf)}))
    assert rc == 1


def test_dict_with_repos_key_fails(tmp_path):
    rf = _repos_file(tmp_path, {"repos": []})
    rc, _ = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(rf)}))
    assert rc == 1


def test_empty_list_fails(tmp_path):
    rf = _repos_file(tmp_path, [])
    rc, _ = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(rf)}))
    assert rc == 1


def test_empty_list_falls_back_to_navigator_repo_like_the_daemon(tmp_path):
    # build_navigator: settings.repos == [] -> falls back to NAVIGATOR_REPO
    repo = _git_repo(tmp_path)
    rf = _repos_file(tmp_path, [])
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(rf),
                                         "NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text
    assert f"ok   repo {repo}" in text


def test_empty_repos_file_falls_back_to_navigator_repo(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": "",
                                         "NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text


def test_effective_repos_rules():
    a = RepoConfig(path="/a")
    assert effective_repos(Settings(repos=[a], repo="/b")) == [a]
    assert [r.path for r in effective_repos(Settings(repos=[], repo="/b"))] == ["/b"]
    assert effective_repos(Settings()) == []


def test_build_navigator_uses_effective_repos(monkeypatch):
    import navigator.__main__ as m
    seen = []

    def recorder(settings):
        seen.append(settings)
        return []

    monkeypatch.setattr(m, "effective_repos", recorder)
    s = Settings(repo="/somewhere")
    # repo is set, so only the recorder's [] can make this raise
    with pytest.raises(ValueError):
        m.build_navigator(s)
    assert seen == [s]


def test_tilde_repo_is_not_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _git_repo(home, "x")
    monkeypatch.setenv("HOME", str(home))
    # with expansion this path would be a valid git repo
    assert (Path("~/x").expanduser() / ".git").exists()
    plist = _plist(tmp_path, None, top={"Label": "l", "WorkingDirectory": str(home),
                                        "EnvironmentVariables": {"NAVIGATOR_REPO": "~/x",
                                                                 "HOME": str(home)}})
    rc, text = _doctor(plist)
    assert rc == 1
    assert "FAIL repo missing: ~/x" in text


def test_relative_paths_resolve_against_working_directory(tmp_path, monkeypatch):
    wd = tmp_path / "wd"
    wd.mkdir()
    _git_repo(wd, "repo")
    _repos_file(wd, [{"path": "repo"}])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    env = {"NAVIGATOR_REPOS_FILE": "repos.json"}
    rc, text = _doctor(_plist(tmp_path, None, top={"Label": "l", "WorkingDirectory": str(wd),
                                                   "EnvironmentVariables": env}))
    assert rc == 0, text
    assert "ok   repo repo" in text
    assert os.getcwd() == str(elsewhere)


def test_relative_paths_not_resolved_against_shell_cwd(tmp_path, monkeypatch):
    # no WorkingDirectory: launchd runs the daemon from /, not the operator's shell cwd
    _git_repo(tmp_path, "repo")
    _repos_file(tmp_path, [{"path": "repo"}])
    monkeypatch.chdir(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": "repos.json"}))
    assert rc == 1
    assert "FAIL settings: FileNotFoundError" in text
    assert os.getcwd() == str(tmp_path)


def test_missing_working_directory_fails(tmp_path):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, None, top={"Label": "l", "WorkingDirectory": str(tmp_path / "nope"),
                                        "EnvironmentVariables": {"NAVIGATOR_REPO": str(repo)}})
    rc, text = _doctor(plist)
    assert rc == 1 and "FAIL workingdir" in text


def test_settings_error_reports_type_only(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_MAX_COST_USD": "-1"}))
    assert rc == 1
    assert "FAIL settings: ValueError\n" in text
    assert "must be" not in text


def test_unknown_driver_fails(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_DRIVER": "bogus"}))
    assert rc == 1 and "FAIL driver" in text


def test_fake_driver_skips_sdk_check(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(cli, "_sdk_version", lambda: None)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_DRIVER": "fake"}))
    assert rc == 0, text
    assert "skip claude-agent-sdk" in text
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_DRIVER": "claude_sdk"}))
    assert rc == 1 and "not installed" in text


def test_url_userinfo_is_redacted(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {
        "NAVIGATOR_REPO": str(repo),
        "NAVIGATOR_ACT_BASE_URL": "http://alice:hunter2@act.local:8090/x"}))
    assert rc == 0, text
    assert "hunter2" not in text and "alice" not in text
    assert "act.local:8090" in text


def test_extended_sensitive_markers(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_DB_PASS": SECRET_KEY,
                                         "NAVIGATOR_SESSION_ID": SECRET_AUTH}))
    assert rc == 0, text
    assert SECRET_KEY not in text and SECRET_AUTH not in text


def test_non_string_plist_value_warns_without_crash(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_TICK_SECONDS": 900}))
    assert rc == 0, text
    assert "WARN" in text and "NAVIGATOR_TICK_SECONDS" in text


def test_shell_env_does_not_leak_into_plist_check(tmp_path, monkeypatch):
    # without isolation the loader would see this [] file and fail
    bad = _repos_file(tmp_path, [], name="shell_repos.json")
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(bad))
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text


def test_secrets_never_printed_on_success(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "ANTHROPIC_API_KEY": SECRET_KEY,
                                         "MY_AUTH": SECRET_AUTH,
                                         "PATH": "/usr/bin:/bin"}))
    assert rc == 0, text
    assert SECRET_KEY not in text and SECRET_AUTH not in text
    assert "ANTHROPIC_API_KEY=<redacted>" in text
    assert "PATH=/usr/bin:/bin" in text


def test_secrets_never_printed_on_error(tmp_path):
    rc, text = _doctor(_plist(tmp_path, {
        "NAVIGATOR_REPOS_FILE": str(tmp_path / "missing.json"),
        "ANTHROPIC_API_KEY": SECRET_KEY, "MY_AUTH": SECRET_AUTH}))
    assert rc == 1
    assert SECRET_KEY not in text and SECRET_AUTH not in text


def test_navigator_token_key_is_redacted(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_TELEGRAM_BOT_TOKEN": SECRET_KEY}))
    assert rc == 0, text
    assert SECRET_KEY not in text


def test_non_dict_top_level_fails(tmp_path):
    rc, _ = _doctor(_plist(tmp_path, None, top=["not", "a", "dict"]))
    assert rc == 1


def test_non_dict_environment_variables_fails(tmp_path):
    rc, _ = _doctor(_plist(tmp_path, None, top={"EnvironmentVariables": ["x"]}))
    assert rc == 1


def test_missing_plist_fails(tmp_path):
    rc, text = _doctor(tmp_path / "nope.plist")
    assert rc == 1 and "FileNotFoundError" in text


def test_sdk_version_compared_as_integers(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo), "NAVIGATOR_DRIVER": "claude_sdk"})
    monkeypatch.setattr(cli, "_sdk_version", lambda: "0.2.9")
    assert _doctor(plist)[0] == 1
    monkeypatch.setattr(cli, "_sdk_version", lambda: "0.2.200")
    assert _doctor(plist)[0] == 0
    monkeypatch.setattr(cli, "_sdk_version", lambda: None)
    assert _doctor(plist)[0] == 1


def test_doctor_is_read_only(tmp_path):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo), "OTHER": "v"})
    mtime = plist.stat().st_mtime_ns
    before = dict(os.environ)
    cwd = os.getcwd()
    _doctor(plist)
    assert dict(os.environ) == before
    assert os.getcwd() == cwd
    assert plist.stat().st_mtime_ns == mtime


def test_environ_and_cwd_restored_when_loader_raises(tmp_path):
    plist = _plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(tmp_path / "missing.json")})
    before = dict(os.environ)
    cwd = os.getcwd()
    rc, text = _doctor(plist)
    assert rc == 1
    assert "FAIL settings: FileNotFoundError" in text
    assert dict(os.environ) == before
    assert os.getcwd() == cwd


def test_doctor_bad_args_usage():
    out = io.StringIO()
    assert run_cli(["doctor", "--bogus"], act=None, out=out) == 2


# ---- C5h: _safe never prints URL secrets --------------------------------------

# (leaky value, substrings that must never be printed)
LEAKY = [
    ("http://h.local/x?token=TOKSECRET1", ("TOKSECRET1", "token=")),
    ("https://hooks.slack.com/services/T000/B000/HOOKSECRET2", ("HOOKSECRET2", "T000", "B000")),
    ("http://u:p/ssSECRET3@h", ("ssSECRET3", "p/ss", "u:p")),
    ("http://user:pa ssSECRET4@h", ("ssSECRET4", "pa ss", "user:")),
    ("u:pwSECRET5@h:5432", ("pwSECRET5",)),
]


@pytest.mark.parametrize("value,secrets", LEAKY)
def test_safe_drops_secret(value, secrets):
    shown = cli._safe(value)
    for s in secrets:
        assert s not in shown


@pytest.mark.parametrize("value,secrets", LEAKY)
def test_doctor_output_never_contains_secret(tmp_path, value, secrets):
    repo = _git_repo(tmp_path)
    _, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                        "NAVIGATOR_WEBHOOK_URL": value}))
    for s in secrets:
        assert s not in text


def test_safe_exact_outputs():
    assert cli._safe("http://127.0.0.1:8090") == "http://127.0.0.1:8090"
    assert cli._safe("https://h:9/x?y=1") == "https://h:9/..."


def test_doctor_prints_plain_url_unchanged(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_WEBHOOK_URL": "http://127.0.0.1:8090"}))
    assert "NAVIGATOR_WEBHOOK_URL=http://127.0.0.1:8090\n" in text


def test_credentialed_navigator_repo_not_printed(tmp_path):
    value = "https://bob:REPOSECRET6@github.com/o/r.git"
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": value}))
    assert rc == 1
    assert "REPOSECRET6" not in text and "bob" not in text
    assert cli._safe(value).find("REPOSECRET6") == -1


def test_driver_with_at_sign_not_printed(tmp_path):
    repo = _git_repo(tmp_path)
    value = "x:DRVSECRET7@h"
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "NAVIGATOR_DRIVER": value}))
    assert rc == 1
    assert "DRVSECRET7" not in text
    assert "DRVSECRET7" not in cli._safe(value)


def test_environ_restored_even_if_chdir_back_fails(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo), "ONLY_IN_PLIST": "1"})
    cwd = os.getcwd()
    monkeypatch.chdir(cwd)  # teardown restores the cwd
    real_chdir = os.chdir
    calls = []

    def flaky_chdir(path):
        calls.append(path)
        if len(calls) == 2:  # the chdir back inside _settings_with_env
            raise OSError("chdir back failed")
        real_chdir(path)

    before = dict(os.environ)
    monkeypatch.setattr(os, "chdir", flaky_chdir)
    try:
        rc, text = _doctor(plist)
    finally:
        real_chdir(cwd)
    assert len(calls) >= 2
    assert rc == 1
    assert "FAIL settings: OSError" in text
    assert dict(os.environ) == before
    assert "ONLY_IN_PLIST" not in os.environ


# ---- stale pending merge approvals -------------------------------------------

STALE_ENV = "NAVIGATOR_STALE_APPROVAL_HOURS"


@pytest.fixture
def _no_stale_env(monkeypatch):
    monkeypatch.delenv(STALE_ENV, raising=False)


def _act_with_pending_merge():
    act = FakeACTClient()
    run = act.create_run(Run(proposal_id="p", base_commit_sha="abc", branch="nav/run-x"))
    approval = act.create_approval(target=run.id, gate=ApprovalGate.merge)
    return act, run, approval


def _doctor_with_act(tmp_path, repo, act):
    out = io.StringIO()
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo)})
    rc = run_cli(["doctor", "--plist", str(plist)], act=act, out=out)
    return rc, out.getvalue()


def _stale_lines(text):
    return [line for line in text.splitlines() if line.startswith(cli._STALE_PREFIX)]


def _two_days_later():
    return datetime.now(timezone.utc) + timedelta(hours=48)


def test_doctor_without_act_skips_stale_check(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text
    assert "skip stale merge approvals (no ACT client)" in text
    assert _stale_lines(text) == []


def test_fresh_pending_approval_is_not_flagged(tmp_path, _no_stale_env):
    repo = _git_repo(tmp_path)
    act, run, _ = _act_with_pending_merge()
    rc, text = _doctor_with_act(tmp_path, repo, act)
    assert rc == 0, text
    assert _stale_lines(text) == []
    assert "no pending merge approval older than 24h" in text


def test_stale_pending_approval_warns_once_naming_the_run(tmp_path, monkeypatch, _no_stale_env):
    repo = _git_repo(tmp_path)
    act, run, approval = _act_with_pending_merge()
    monkeypatch.setattr(cli, "_now", _two_days_later)
    rc, text = _doctor_with_act(tmp_path, repo, act)
    assert rc == 0, text  # a warning, never a doctor failure
    lines = _stale_lines(text)
    assert len(lines) == 1
    assert run.id in lines[0] and approval.id in lines[0]
    assert "> 24h" in lines[0]
    assert "doctor: ok" in text


def test_decided_approval_is_not_flagged(tmp_path, monkeypatch, _no_stale_env):
    repo = _git_repo(tmp_path)
    act, run, approval = _act_with_pending_merge()
    act.decide_approval(approval.id, ApprovalDecision.approved, actor=cli.ACTOR)
    monkeypatch.setattr(cli, "_now", _two_days_later)
    _, text = _doctor_with_act(tmp_path, repo, act)
    assert _stale_lines(text) == []


def test_stale_threshold_env_override(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    act, run, _ = _act_with_pending_merge()
    monkeypatch.setattr(cli, "_now", _two_days_later)
    monkeypatch.setenv(STALE_ENV, "100")
    _, text = _doctor_with_act(tmp_path, repo, act)
    assert _stale_lines(text) == []
    assert "older than 100h" in text
    monkeypatch.setenv(STALE_ENV, "1")
    _, text = _doctor_with_act(tmp_path, repo, act)
    lines = _stale_lines(text)
    assert len(lines) == 1 and run.id in lines[0] and "> 1h" in lines[0]


@pytest.mark.parametrize("raw", ["abc", "-3", "0", "inf", "nan"])
def test_unusable_stale_threshold_warns_and_uses_default(tmp_path, monkeypatch, raw):
    repo = _git_repo(tmp_path)
    act, run, _ = _act_with_pending_merge()
    monkeypatch.setattr(cli, "_now", _two_days_later)
    monkeypatch.setenv(STALE_ENV, raw)
    rc, text = _doctor_with_act(tmp_path, repo, act)
    assert rc == 0, text
    assert f"WARN {STALE_ENV}: not a positive number, using 24h" in text
    assert len(_stale_lines(text)) == 1


def test_stale_check_is_read_only(tmp_path, monkeypatch, _no_stale_env):
    repo = _git_repo(tmp_path)
    act, run, approval = _act_with_pending_merge()
    monkeypatch.setattr(cli, "_now", _two_days_later)
    _doctor_with_act(tmp_path, repo, act)
    pending = act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                 decision=ApprovalDecision.pending)
    assert [a.id for a in pending] == [approval.id]
    assert pending[0].decision == ApprovalDecision.pending


def test_act_failure_in_stale_check_prints_class_name_only(tmp_path, _no_stale_env):
    repo = _git_repo(tmp_path)

    class Broken:
        def list_open_runs(self):
            raise RuntimeError("http://u:SECRETX@act.local")

    rc, text = _doctor_with_act(tmp_path, repo, Broken())
    assert rc == 0, text
    assert "WARN stale merge approvals unreadable: RuntimeError" in text
    assert "SECRETX" not in text


def test_approval_created_at_comes_from_ulid_id():
    act, _, approval = _act_with_pending_merge()
    created = cli._approval_created_at(approval)
    assert created is not None and created.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - created).total_seconds()) < 60

    class NoId:
        id = "approval-not-a-ulid"

    assert cli._approval_created_at(NoId()) is None


# ---- backlog (per repo, like the daemon) + duplicate telegram readers ---------

ACT_URL = "http://act.local:8090"


@pytest.fixture(autouse=True)
def _no_real_launch_agents(monkeypatch, tmp_path):
    # never read the real ~/Library/LaunchAgents from a test
    monkeypatch.setattr(cli, "_launch_agents_dir", lambda: tmp_path / "LaunchAgents")


def _backlog_env(repo):
    return {"NAVIGATOR_REPO": str(repo), "NAVIGATOR_ACT_BASE_URL": ACT_URL,
            "NAVIGATOR_MAX_OPEN_PROPOSALS": "3"}


def _doctor_env(tmp_path, env, act):
    out = io.StringIO()
    plist = _plist(tmp_path, env)
    rc = run_cli(["doctor", "--plist", str(plist)], act=act, out=out)
    return rc, out.getvalue()


def _proposal(repo, created_at=None):
    return Proposal(origin="t", proposal_origin_hash="h", title="t", rationale="r",
                    repo=repo, base_commit_sha="abc", intended_changes="x",
                    created_at=created_at or datetime.now(timezone.utc))


def _act_with(repo, n_mine, n_other=0, created_at=None):
    act = FakeACTClient()
    for _ in range(n_mine):
        act.create_proposal(_proposal(str(repo), created_at))
    for _ in range(n_other):
        act.create_proposal(_proposal("/other/repo", created_at))
    return act


def _backlog_lines(text):
    return [line for line in text.splitlines() if line.startswith(cli._BACKLOG_PREFIX)]


def _duplicate_lines(text):
    return [line for line in text.splitlines()
            if line.startswith(cli._DUPLICATE_READERS_PREFIX)]


def test_backlog_counts_only_this_repo(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor_env(tmp_path, _backlog_env(repo), _act_with(repo, 3, 5))
    assert rc == 0, text
    lines = _backlog_lines(text)
    assert len(lines) == 1
    assert "3 open proposals >= cap 3" in lines[0]
    rc, text = _doctor_env(tmp_path, _backlog_env(repo), _act_with(repo, 2, 5))
    assert rc == 0, text
    assert _backlog_lines(text) == []
    ok = [line for line in text.splitlines() if line.startswith("ok   backlog")]
    assert len(ok) == 1 and "2/3 open" in ok[0]


def test_backlog_ignores_proposals_older_than_ttl(tmp_path):
    repo = _git_repo(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=8)  # default TTL is 7 days
    act = _act_with(repo, 3, created_at=old)
    rc, text = _doctor_env(tmp_path, _backlog_env(repo), act)
    assert rc == 0, text
    assert _backlog_lines(text) == []
    ok = [line for line in text.splitlines() if line.startswith("ok   backlog")]
    assert len(ok) == 1 and "0/3 open" in ok[0]
    # doctor expired nothing
    assert len(act.proposals) == 3
    assert all(p.status == ProposalStatus.proposed for p in act.proposals.values())


def test_backlog_skipped_without_daemon_act_url(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor_env(tmp_path, {"NAVIGATOR_REPO": str(repo)}, _act_with(repo, 3))
    assert rc == 0, text
    assert "skip backlog (daemon uses an in-memory ACT" in text
    assert _backlog_lines(text) == []


def test_backlog_url_mismatch_warns_and_does_not_count(tmp_path):
    repo = _git_repo(tmp_path)
    act = _act_with(repo, 3)
    act.base_url = "http://elsewhere:9/api/v1/agent"
    rc, text = _doctor_env(tmp_path, _backlog_env(repo), act)
    assert rc == 0, text
    assert "is not the daemon's" in text
    assert "/3 open" not in text
    assert _backlog_lines(text) == []


def test_backlog_act_error_warns_class_only(tmp_path):
    repo = _git_repo(tmp_path)

    class Broken(FakeACTClient):
        def list_proposals(self, status=None):
            raise RuntimeError("http://u:SECRETY@act.local")

    rc, text = _doctor_env(tmp_path, _backlog_env(repo), Broken())
    assert rc == 0, text
    assert "WARN backlog unreadable: RuntimeError" in text
    assert "SECRETY" not in text


def _launch_agents(tmp_path, *names):
    d = tmp_path / "la"
    d.mkdir()
    for i, name in enumerate(names):
        (d / name).write_text(f"plist {i}\n")
    return d


def test_duplicate_readers_warns_with_real_uid(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    d = _launch_agents(tmp_path, cli._LEGACY_READER_PLIST, cli._ONEBOT_PLIST)
    monkeypatch.setattr(cli, "_launch_agents_dir", lambda: d)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text
    lines = _duplicate_lines(text)
    assert len(lines) == 1
    assert f"gui/{os.getuid()}/com.calwbot.navigator-telegram" in lines[0]
    if os.getuid() != 501:
        assert "gui/501/" not in lines[0]
    assert "doctor: ok" in text


def test_renamed_legacy_plist_is_not_counted(tmp_path):
    d = _launch_agents(tmp_path, "com.calwbot.navigator-telegram.plist.disabled-20261021-onebot",
                       "com.calwbot.telegram-bot.plist")
    out = io.StringIO()
    cli._check_duplicate_telegram_readers(out, d)
    text = out.getvalue()
    assert "ok   telegram reader com.calwbot.telegram-bot.plist only" in text
    assert _duplicate_lines(text) == []


def test_duplicate_readers_check_is_read_only(tmp_path):
    d = _launch_agents(tmp_path, cli._LEGACY_READER_PLIST, cli._ONEBOT_PLIST)

    def snapshot():
        return [(p.name, p.read_bytes()) for p in sorted(d.iterdir())]

    before = snapshot()
    out = io.StringIO()
    cli._check_duplicate_telegram_readers(out, d)
    assert len(_duplicate_lines(out.getvalue())) == 1
    assert snapshot() == before


def test_missing_launch_agents_dir_is_ok(tmp_path):
    out = io.StringIO()
    cli._check_duplicate_telegram_readers(out, tmp_path / "nope")
    text = out.getvalue()
    assert "ok   telegram readers: no LaunchAgents dir" in text
    assert _duplicate_lines(text) == []
