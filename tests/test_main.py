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
    assert isinstance(nav.act.inner, FakeACTClient)  # wrapped to announce run events
    assert isinstance(nav.driver, FakeSubagentDriver)

def test_build_navigator_uses_http_act_when_url_set(tmp_path):
    s = _settings(tmp_path, _seed_repo(tmp_path), act_base_url="http://127.0.0.1:9")
    nav = build_navigator(s)
    assert isinstance(nav.act.inner, HTTPACTClient)

def test_build_navigator_requires_repo(tmp_path):
    with pytest.raises(ValueError, match="NAVIGATOR_REPO"):
        build_navigator(Settings(repo=None))

def test_build_navigator_rejects_unknown_driver(tmp_path):
    with pytest.raises(ValueError, match="bogus"):
        build_navigator(_settings(tmp_path, _seed_repo(tmp_path), driver="bogus"))

def test_claude_sdk_driver_requires_sdk_extra(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # simulate sdk extra not installed
    with pytest.raises(ValueError, match="claude-agent-sdk"):
        build_navigator(_settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk"))

def test_build_navigator_wires_claude_sdk_driver(tmp_path, monkeypatch):
    import sys, types
    mod = types.ModuleType("claude_agent_sdk")
    mod.query = None; mod.ClaudeAgentOptions = None; mod.ResultMessage = None
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
    s = _settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk", max_cost_usd=1.5)
    nav = build_navigator(s)
    assert isinstance(nav.driver, ClaudeSDKDriver)
    assert nav.driver.budget_guard.max_cost_usd == 1.5

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
    assert summary["runs_finalized"] == 0     # merge gate is polled every cycle
    assert summary["snapshot_chars"] > 0      # planner prompt size is visible per cycle

def test_negative_max_cycles_is_rejected():
    with pytest.raises(SystemExit):
        main(["--max-cycles", "not-a-number"])

def _daemon_env(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path)
    for k in ["NAVIGATOR_DRIVER","NAVIGATOR_MODE","NAVIGATOR_ACT_BASE_URL","NAVIGATOR_ORC_BASE_URL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO", str(repo))
    monkeypatch.setenv("NAVIGATOR_LOCK_PATH", str(tmp_path/"nav.lock"))
    monkeypatch.setenv("NAVIGATOR_WORKTREE_ROOT", str(tmp_path/"wt"))

def test_daemon_runs_bounded_cycles_and_sleeps_interval(tmp_path, monkeypatch, capsys):
    _daemon_env(tmp_path, monkeypatch)
    monkeypatch.setenv("NAVIGATOR_TICK_INTERVAL_S", "123")
    sleeps = []
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    assert main(["--max-cycles", "2"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert [l["cycle"] for l in lines] == [1, 2]
    assert all(l["mode"] == "dry_run" for l in lines)
    assert sleeps == [123]  # sleeps between cycles, not after the last

def test_daemon_is_default_when_once_absent(tmp_path, monkeypatch, capsys):
    _daemon_env(tmp_path, monkeypatch)
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    assert main(["--max-cycles", "1"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["cycle"] == 1

def test_daemon_survives_a_failing_cycle(tmp_path, monkeypatch, capsys):
    # live regression 2026-07-13: ORC restart mid-boot → ConnectError killed the daemon
    _daemon_env(tmp_path, monkeypatch)
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = {"n": 0}
    real_cycle = m._cycle
    def flaky_cycle(nav, settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("dependency down")
        return real_cycle(nav, settings)
    monkeypatch.setattr(m, "_cycle", flaky_cycle)
    assert main(["--max-cycles", "2"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert "error" in lines[0] and "dependency down" in lines[0]["error"]
    assert lines[1]["cycle"] == 2 and "error" not in lines[1]

def test_daemon_resets_budget_each_cycle(tmp_path, monkeypatch):
    # live regression 2026-07-14: process-lifetime budget exhausted after ~8 cycles,
    # then every cycle failed BudgetExceeded. Budget must reset per tick.
    from navigator.safety.budget_guard import BudgetGuard
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    guard = BudgetGuard(2.0)
    class FakeDriver: pass
    driver = FakeDriver(); driver.budget_guard = guard
    class FakeNav:
        def __init__(self): self.driver = driver
        def reconcile_on_startup(self): pass
    nav = FakeNav()
    seen = []
    def fake_cycle(n, s):
        seen.append(n.driver.budget_guard.spent_usd)  # spend at cycle entry
        n.driver.budget_guard.record_spend(2.0)       # exhaust within the cycle
        return {}
    monkeypatch.setattr(m, "_cycle", fake_cycle)
    m.run_daemon(nav, Settings(), max_cycles=3)
    assert seen == [0.0, 0.0, 0.0]  # reset before every cycle despite exhausting each

def test_once_and_max_cycles_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["--once", "--max-cycles", "2"])

def test_operator_commands_dispatch_to_cli_against_act(monkeypatch, capsys):
    import navigator.__main__ as m
    seen = {}
    class _ACT(FakeACTClient):
        def __init__(self, url):
            super().__init__(); seen["url"] = url
    monkeypatch.setattr(m, "HTTPACTClient", _ACT)
    monkeypatch.delenv("NAVIGATOR_ACT_BASE_URL", raising=False)
    assert main(["status"]) == 0
    assert seen["url"] == "http://127.0.0.1:8090"  # local ACT default for operator use
    assert "proposed (0)" in capsys.readouterr().out

def test_daemon_health_alert_once_per_failure_streak_then_recovery(tmp_path, monkeypatch, capsys):
    # 2026-08-25 → 10-09: 216 consecutive failed cycles went unnoticed for six weeks
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    sent = []
    class _Rec:
        def send(self, text): sent.append(text)
    nav = build_navigator(_settings(tmp_path, _seed_repo(tmp_path)))
    nav.notifier = _Rec()
    outcomes = iter([False] * 6 + [True, False])
    def fake_cycle(n, s):
        if not next(outcomes):
            raise RuntimeError("Usage credits are required")
        return {"ok": 1}
    monkeypatch.setattr(m, "_cycle", fake_cycle)
    m.run_daemon(nav, Settings(), max_cycles=8)
    alerts = [t for t in sent if "failing" in t]
    assert len(alerts) == 1 and "Usage credits" in alerts[0]
    assert sum("recovered" in t for t in sent) == 1

def test_structured_logs_go_to_stderr_keeping_stdout_json_only(tmp_path, monkeypatch, capsys):
    import navigator.__main__ as m
    from navigator.observability.logging import get_logger
    m._configure_logging()
    get_logger("t").info("tick.backlog_full", open=99)
    captured = capsys.readouterr()
    assert "tick.backlog_full" not in captured.out
    assert "tick.backlog_full" in captured.err
