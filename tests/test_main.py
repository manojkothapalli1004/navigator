import json
import subprocess
import pytest
from navigator.__main__ import build_navigator, main
from navigator.config import Settings
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver

def _seed_repo(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    return repo

def _settings(tmp_path, repo, **kw):
    return Settings(repo=str(repo), lock_path=str(tmp_path/"nav.lock"),
                    worktree_root=str(tmp_path/"wt"), **kw)

def test_build_navigator_defaults_to_fake_clients(tmp_path):
    nav = build_navigator(_settings(tmp_path, _seed_repo(tmp_path)))
    assert isinstance(nav.act, FakeACTClient)
    assert isinstance(nav.driver, FakeSubagentDriver)

def test_build_navigator_uses_http_act_when_url_set(tmp_path):
    s = _settings(tmp_path, _seed_repo(tmp_path), act_base_url="http://127.0.0.1:9")
    nav = build_navigator(s)
    assert isinstance(nav.act, HTTPACTClient)

def test_build_navigator_requires_repo(tmp_path):
    with pytest.raises(ValueError, match="NAVIGATOR_REPO"):
        build_navigator(Settings(repo=None))

def test_build_navigator_rejects_unknown_driver(tmp_path):
    with pytest.raises(ValueError, match="bogus"):
        build_navigator(_settings(tmp_path, _seed_repo(tmp_path), driver="bogus"))

def test_claude_sdk_driver_requires_sdk_extra(tmp_path):
    with pytest.raises(ValueError, match="claude-agent-sdk"):
        build_navigator(_settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk"))

def test_main_once_prints_summary_and_exits_zero(tmp_path, monkeypatch, capsys):
    repo = _seed_repo(tmp_path)
    for k in ["NAVIGATOR_DRIVER","NAVIGATOR_MODE","NAVIGATOR_ACT_BASE_URL","NAVIGATOR_ORC_BASE_URL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO", str(repo))
    monkeypatch.setenv("NAVIGATOR_LOCK_PATH", str(tmp_path/"nav.lock"))
    monkeypatch.setenv("NAVIGATOR_WORKTREE_ROOT", str(tmp_path/"wt"))
    assert main(["--once"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry_run"
    assert summary["proposals_created"] == 1  # fake planner proposes exactly one
    assert summary["runs_executed"] == 0      # dry_run never executes

def test_main_without_once_is_rejected():
    with pytest.raises(SystemExit):
        main([])
