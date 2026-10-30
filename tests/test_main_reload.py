"""U2a settings hot reload: the daemon overlays the ACT document navigator/settings on its
environment Settings at the top of each cycle (hot fields only, env-only fields refused, the
ClaudeSDKDriver rebuilt keeping cooldowns and budget) and writes navigator/status after each
cycle. With no document the cycle lines are byte-identical to before."""
import json
import sys
import types

import httpx
import pytest

import navigator.__main__ as m
from navigator.__main__ import build_navigator, run_daemon
from navigator.config import RepoConfig
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from tests.test_main import _cycle_lines, _seed_repo, _settings

SETTINGS_DOC = "navigator/settings"
STATUS_DOC = "navigator/status"
_OK_PLAN = json.dumps({"proposals": [], "summary": "nothing to do"})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)


class _Rec:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _nav(tmp_path, **kw):
    s = _settings(tmp_path, _seed_repo(tmp_path), **kw)
    nav = build_navigator(s)
    return nav, nav.act.inner, s  # the FakeACTClient behind NotifyingACT


def _sdk_nav(tmp_path, monkeypatch, **kw):
    mod = types.ModuleType("claude_agent_sdk")
    mod.query = None; mod.ClaudeAgentOptions = None; mod.ResultMessage = None
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr(m, "make_sdk_query_fn",
                        lambda *, model, timeout_s, **kw_: (lambda prompt, **k: _OK_PLAN))
    return _nav(tmp_path, driver="claude_sdk", sdk_model="claude-fable-5-1", **kw)


def _run(nav, s, capsys, n=1):
    run_daemon(nav, s, max_cycles=n)
    return _cycle_lines(capsys.readouterr().out)


def _after_cycle(monkeypatch, hook):
    real = m._cycle

    def cycle(nav, s):
        summary = real(nav, s)
        hook()
        return summary
    monkeypatch.setattr(m, "_cycle", cycle)


def test_no_doc_is_byte_identical(tmp_path, capsys):
    nav, act, s = _nav(tmp_path)
    lines = _run(nav, s, capsys, n=2)
    assert [l["cycle"] for l in lines] == [1, 2]
    assert not any(k.startswith("settings") for l in lines for k in l)
    assert all(l["mode"] == "dry_run" for l in lines)
    assert nav.settings is s


def test_reload_applies_new_version_at_cycle_start(tmp_path, capsys):
    nav, act, s = _nav(tmp_path)
    act.put_doc(SETTINGS_DOC, {"mode": "execute"}, actor="ui")
    lines = _run(nav, s, capsys)
    assert lines[0]["mode"] == "execute"
    assert lines[0]["settings_version"] == 1
    assert "settings_error" not in lines[0] and "settings_rejected" not in lines[0]
    assert nav.settings.mode == "execute"
    assert nav.settings.driver == "fake"  # env-only fields come from the environment


def test_doc_removed_returns_to_env(tmp_path, monkeypatch, capsys):
    nav, act, s = _nav(tmp_path)
    act.put_doc(SETTINGS_DOC, {"mode": "execute"}, actor="ui")
    _after_cycle(monkeypatch, lambda: act.docs.pop(SETTINGS_DOC, None))
    lines = _run(nav, s, capsys, n=2)
    assert lines[0]["mode"] == "execute" and lines[0]["settings_version"] == 1
    assert lines[1]["mode"] == "dry_run" and "settings_version" not in lines[1]
    assert nav.settings == s and nav.settings.mode == "dry_run"


def test_reload_keeps_settings_on_act_error(tmp_path, monkeypatch, capsys):
    nav, act, s = _nav(tmp_path)
    rec = _Rec()
    nav.notifier = rec

    def boom(key):
        req = httpx.Request("GET", f"http://act/api/v1/agent/docs/{key}")
        raise httpx.HTTPStatusError("500 Internal Server Error", request=req,
                                    response=httpx.Response(500, request=req))
    monkeypatch.setattr(act, "get_doc", boom)
    lines = _run(nav, s, capsys, n=4)
    for line in lines:
        assert line["settings_error"].startswith("HTTPStatusError")
        assert "error" not in line           # NEGATIVE: not a failed cycle
        assert "consecutive_failures" not in line
        assert line["mode"] == "dry_run"
    assert nav.settings is s
    assert not any("failing" in t for t in rec.sent)  # no failure-streak alert


def test_rejected_keys_reported(tmp_path, capsys):
    nav, act, s = _nav(tmp_path)
    act.put_doc(SETTINGS_DOC, {"allowed_commands": ["x"], "mode": "execute"}, actor="ui")
    lines = _run(nav, s, capsys)
    assert lines[0]["settings_rejected"] == ["allowed_commands: env-only"]
    assert lines[0]["mode"] == "execute"
    assert nav.settings.mode == "execute"
    assert nav.settings.allowed_commands == s.allowed_commands
    status = act.get_doc(STATUS_DOC)[0]
    assert status["settings_rejected"] == ["allowed_commands: env-only"]


def test_rebuild_driver_keeps_cooldowns(tmp_path, monkeypatch, capsys):
    nav, act, s = _sdk_nav(tmp_path, monkeypatch)
    old = nav.driver
    assert isinstance(old, ClaudeSDKDriver)
    old.cooldowns["claude-fable-5-1"] = 123.0
    act.put_doc(SETTINGS_DOC, {"sdk_model": "claude-opus-5-5"}, actor="ui")
    lines = _run(nav, s, capsys)
    assert "settings_error" not in lines[0]
    assert nav.driver is not old
    assert isinstance(nav.driver, ClaudeSDKDriver)
    assert nav.driver.cooldowns is old.cooldowns
    assert nav.driver.cooldowns == {"claude-fable-5-1": 123.0}
    assert nav.driver.budget_guard is old.budget_guard
    assert nav.settings.sdk_model == "claude-opus-5-5"
    assert nav.triage.query_fn is nav.driver.query_fn  # triage ranks with the new driver


def test_non_wiring_change_keeps_driver(tmp_path, monkeypatch, capsys):
    nav, act, s = _sdk_nav(tmp_path, monkeypatch)
    old = nav.driver
    act.put_doc(SETTINGS_DOC, {"max_open_proposals": 3}, actor="ui")
    _run(nav, s, capsys)
    assert nav.settings.max_open_proposals == 3
    assert nav.driver is old


def test_rebuild_failure_keeps_old_driver(tmp_path, monkeypatch, capsys):
    nav, act, s = _sdk_nav(tmp_path, monkeypatch)  # builder_tools stays False
    old = nav.driver
    act.put_doc(SETTINGS_DOC, {"builder_tests": True, "mode": "execute"}, actor="ui")
    lines = _run(nav, s, capsys)
    err = lines[0]["settings_error"]
    assert "NAVIGATOR_BUILDER_TESTS needs" in err
    assert "error" not in lines[0]
    assert nav.driver is old
    assert nav.settings.mode == "execute"
    assert nav.settings.builder_tests is False
    status = act.get_doc(STATUS_DOC)[0]
    assert status["effective"]["builder_tests"] is False
    assert status["effective"]["mode"] == "execute"
    assert status["settings_error"] == err
    # NEGATIVE: a failed wiring change never sneaks in on a later version
    act.put_doc(SETTINGS_DOC, {"builder_tests": True, "max_open_proposals": 3}, actor="ui")
    lines = _run(nav, s, capsys)
    assert lines[0]["settings_version"] == 2
    assert lines[0]["settings_error"] == err
    assert nav.driver is old
    assert nav.settings.builder_tests is False
    assert nav.settings.max_open_proposals == 3
    assert act.get_doc(STATUS_DOC)[0]["effective"]["builder_tests"] is False


def test_rebuild_unexpected_error_does_not_kill_daemon(tmp_path, monkeypatch, capsys):
    nav, act, s = _sdk_nav(tmp_path, monkeypatch)
    old = nav.driver

    def exploding(**kw):
        raise RuntimeError("sdk wiring exploded")
    monkeypatch.setattr(m, "make_sdk_query_fn", exploding)
    act.put_doc(SETTINGS_DOC, {"sdk_model": "claude-opus-5-5"}, actor="ui")
    assert run_daemon(nav, s, max_cycles=2) is None
    lines = _cycle_lines(capsys.readouterr().out)
    assert [l["cycle"] for l in lines] == [1, 2]
    assert lines[0]["settings_error"].startswith("RuntimeError")
    assert "error" not in lines[0]
    assert nav.driver is old
    assert nav.settings.sdk_model == "claude-fable-5-1"  # the wiring field did not apply


def test_status_doc_written_each_cycle(tmp_path, capsys):
    nav, act, s = _nav(tmp_path)
    _run(nav, s, capsys, n=2)
    body, version = act.get_doc(STATUS_DOC)
    assert version == 2  # one write per cycle
    assert body["cycle"] == 2
    assert body["effective"]["mode"] == "dry_run"
    assert set(body["effective"]) == set(m.HOT_FIELDS)
    assert "lock_path" in body["env_only"]
    assert set(body["env_only"]) == set(m.ENV_ONLY_FIELDS)
    assert all(isinstance(v, str) for v in body["env_only"].values())
    assert json.loads(body["env_only"]["allowed_commands"]) == s.allowed_commands
    assert body["settings_version"] is None
    assert body["settings_error"] is None and body["settings_rejected"] == []
    assert body["summary"]["cycle"] if "cycle" in body["summary"] else body["summary"]["mode"] == "dry_run"
    assert act.docs[STATUS_DOC][2] == "navigator-daemon"


def test_status_write_failure_reported(tmp_path, monkeypatch, capsys):
    nav, act, s = _nav(tmp_path)

    def failing_put(key, body, actor, expected_version=None):
        raise OSError("disk full")
    monkeypatch.setattr(act, "put_doc", failing_put)
    lines = _run(nav, s, capsys, n=2)
    assert [l["cycle"] for l in lines] == [1, 2]
    for line in lines:
        assert line["status_write_error"] == "OSError"
        assert "error" not in line


def test_fleet_members_all_get_settings(tmp_path, capsys):
    a, b = _seed_repo(tmp_path / "a"), _seed_repo(tmp_path / "b")
    s = _settings(tmp_path, a, repos=[RepoConfig(path=str(a)), RepoConfig(path=str(b))])
    fleet = build_navigator(s)
    assert len(fleet.navigators) == 2
    act = fleet.navigators[0].act.inner
    act.put_doc(SETTINGS_DOC, {"mode": "execute"}, actor="ui")
    lines = _run(fleet, s, capsys)
    assert lines[0]["mode"] == "execute" and lines[0]["settings_version"] == 1
    assert [n.settings.mode for n in fleet.navigators] == ["execute", "execute"]
    assert fleet.navigators[0].settings is fleet.navigators[1].settings


def test_tick_interval_from_document(tmp_path, monkeypatch, capsys):
    nav, act, s = _nav(tmp_path)
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    act.put_doc(SETTINGS_DOC, {"tick_interval_s": 120}, actor="ui")
    _run(nav, s, capsys, n=2)
    assert sleeps == [120]


def test_same_version_new_body_is_reapplied(tmp_path, monkeypatch, capsys):
    nav, act, s = _nav(tmp_path)
    act.put_doc(SETTINGS_DOC, {"mode": "execute"}, actor="ui")

    def recreate_at_same_version():
        act.docs[SETTINGS_DOC] = ({"mode": "dry_run"}, 1, "ui")
    _after_cycle(monkeypatch, recreate_at_same_version)
    lines = _run(nav, s, capsys, n=2)
    assert lines[0]["mode"] == "execute"
    assert lines[1]["mode"] == "dry_run"
    assert lines[0]["settings_version"] == lines[1]["settings_version"] == 1
    assert nav.settings.mode == "dry_run"


def test_unchanged_doc_is_not_reapplied_and_runner_timeout_follows_settings(tmp_path, monkeypatch,
                                                                          capsys):
    nav, act, s = _nav(tmp_path, command_timeout_s=900.0)
    assert nav.runner.timeout_s == 900.0
    act.put_doc(SETTINGS_DOC, {"mode": "execute"}, actor="ui")
    applied = []
    real_apply = m._apply_settings
    monkeypatch.setattr(m, "_apply_settings",
                        lambda t, new, old: applied.append(new.mode) or real_apply(t, new, old))
    lines = _run(nav, s, capsys, n=3)
    assert applied == ["execute"]  # applied once, not every cycle
    assert all(l["mode"] == "execute" and l["settings_version"] == 1 for l in lines)
    assert nav.runner.timeout_s == 900.0  # the fake driver: builder_tests stays off, no clamp
