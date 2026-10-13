import io
import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from navigator import cli
from navigator.cli import run_cli
from navigator.config import RepoConfig, Settings, effective_repos

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
