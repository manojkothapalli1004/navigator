import io
import json
import os
import plistlib
import subprocess

import pytest

from navigator import cli
from navigator.cli import run_cli

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


def test_empty_repos_file_falls_back_to_navigator_repo(tmp_path):
    repo = _git_repo(tmp_path)
    rc, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPOS_FILE": "",
                                         "NAVIGATOR_REPO": str(repo)}))
    assert rc == 0, text


def test_tilde_repo_is_not_expanded(tmp_path):
    rc, _ = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": "~/x"}))
    assert rc == 1


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
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo)})
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
    _doctor(plist)
    assert dict(os.environ) == before
    assert plist.stat().st_mtime_ns == mtime


def test_environ_restored_when_loader_raises(tmp_path):
    plist = _plist(tmp_path, {"NAVIGATOR_REPOS_FILE": str(tmp_path / "missing.json")})
    before = dict(os.environ)
    rc, _ = _doctor(plist)
    assert rc == 1
    assert dict(os.environ) == before


def test_doctor_bad_args_usage():
    out = io.StringIO()
    assert run_cli(["doctor", "--bogus"], act=None, out=out) == 2
