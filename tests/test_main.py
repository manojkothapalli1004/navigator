import json
import subprocess
from datetime import datetime
import pytest
from navigator.__main__ import build_navigator, main
from navigator.safety.errors import QuotaExhaustedError
from navigator.config import Settings
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver

def _seed_repo(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir(parents=True)
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    return repo

def _settings(tmp_path, repo, **kw):
    return Settings(repo=str(repo), lock_path=str(tmp_path/"nav.lock"),
                    worktree_root=str(tmp_path/"wt"), **kw)

def _cycle_lines(out: str) -> list[dict]:
    # daemon stdout: one startup line, then one JSON summary per cycle
    return [l for l in (json.loads(x) for x in out.strip().splitlines()) if "cycle" in l]

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
    for k in ["NAVIGATOR_DRIVER","NAVIGATOR_MODE","NAVIGATOR_ACT_BASE_URL","NAVIGATOR_ORC_BASE_URL",
              "NAVIGATOR_REPOS_FILE"]:
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
    for k in ["NAVIGATOR_DRIVER","NAVIGATOR_MODE","NAVIGATOR_ACT_BASE_URL","NAVIGATOR_ORC_BASE_URL",
              "NAVIGATOR_REPOS_FILE"]:
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
    lines = _cycle_lines(capsys.readouterr().out)
    assert [l["cycle"] for l in lines] == [1, 2]
    assert all(l["mode"] == "dry_run" for l in lines)
    assert sleeps == [123]  # sleeps between cycles, not after the last

def test_daemon_is_default_when_once_absent(tmp_path, monkeypatch, capsys):
    _daemon_env(tmp_path, monkeypatch)
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    assert main(["--max-cycles", "1"]) == 0
    lines = _cycle_lines(capsys.readouterr().out)
    assert [l["cycle"] for l in lines] == [1]

def test_daemon_survives_a_failing_cycle(tmp_path, monkeypatch, capsys):
    # live regression 2026-07-13: ORC restart mid-boot \u2192 ConnectError killed the daemon
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
    lines = _cycle_lines(capsys.readouterr().out)
    assert "error" in lines[0] and "dependency down" in lines[0]["error"]
    assert lines[1]["cycle"] == 2 and "error" not in lines[1]

def test_daemon_prints_startup_line_with_code_sha_before_first_cycle(monkeypatch, capsys):
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "code_sha", lambda: "abc123")
    class FakeNav:
        driver = None
        def reconcile_on_startup(self): pass
    monkeypatch.setattr(m, "_cycle", lambda n, s: {"ok": 1})
    m.run_daemon(FakeNav(), Settings(), max_cycles=1)
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["startup"] == "navigator"
    assert lines[0]["code_sha"] == "abc123" and "ts" in lines[0]
    assert lines[1]["cycle"] == 1

def test_daemon_startup_line_tolerates_unknown_code_sha(monkeypatch, capsys):
    import navigator.__main__ as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "code_sha", lambda: None)
    class FakeNav:
        driver = None
        def reconcile_on_startup(self): pass
    monkeypatch.setattr(m, "_cycle", lambda n, s: {"ok": 1})
    m.run_daemon(FakeNav(), Settings(), max_cycles=1)
    first = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert first["startup"] == "navigator" and "code_sha" in first and first["code_sha"] is None

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
    # 2026-08-25 \u2192 10-09: 216 consecutive failed cycles went unnoticed for six weeks
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

def test_planner_failure_does_not_block_execution_or_merges(tmp_path, monkeypatch):
    # live 2026-10-09: the planner hit the account's session limit and the whole
    # cycle aborted, so an approved proposal and merge decisions sat untouched
    import navigator.__main__ as m
    nav = build_navigator(_settings(tmp_path, _seed_repo(tmp_path)))
    calls = []
    def broken_tick(base_commit_sha=None):
        raise RuntimeError("You've hit your session limit")
    monkeypatch.setattr(nav, "periodic_tick", broken_tick)
    monkeypatch.setattr(nav, "approval_poll", lambda: calls.append("exec") or [])
    monkeypatch.setattr(nav, "merge_poll", lambda: calls.append("merge") or [])
    summary = m._cycle(nav, nav.settings)
    assert calls == ["exec", "merge"]
    assert "session limit" in summary["planner_error"]

def test_per_role_sdk_models_are_wired(tmp_path, monkeypatch):
    # operator 2026-10-10: use cheaper models where Opus isn't needed
    import navigator.__main__ as m
    from navigator.contracts.enums import Role
    built = []
    monkeypatch.setattr(m, "make_sdk_query_fn",
                        lambda model, timeout_s, **kw: built.append(model) or (lambda p: model))
    s = _settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk", sdk_model="claude-opus-5-5",
                  sdk_role_models={"planner": "claude-sonnet-5-5", "verifier": "claude-sonnet-5-5"})
    drv = build_navigator(s).driver
    assert drv.query_fn("x") == "claude-opus-5-5"
    assert drv.role_query_fns[Role.planner]("x") == "claude-sonnet-5-5"
    assert drv.role_query_fns[Role.verifier]("x") == "claude-sonnet-5-5"
    assert Role.builder not in drv.role_query_fns

def test_sdk_model_fallback_chain_is_wired_into_the_daemon(tmp_path, monkeypatch):
    # operator 2026-10-15: NAVIGATOR_SDK_MODEL_FALLBACKS / _COOLDOWN_S must actually
    # reach every query fn the daemon builds, with ONE cooldown dict shared by all
    import navigator.__main__ as m
    built = []
    def fake_make(model, timeout_s, **kw):
        built.append((model, timeout_s, kw))
        return lambda p: model
    monkeypatch.setattr(m, "make_sdk_query_fn", fake_make)
    s = _settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk", sdk_model="claude-fable-5-1",
                  sdk_role_models={"planner": "claude-sonnet-5-5", "verifier": "claude-sonnet-5-5"},
                  sdk_model_fallbacks=["claude-opus-5-5", "claude-sonnet-5-5"],
                  sdk_model_cooldown_s=900, sdk_timeout_s=77.0, max_cost_usd=1.25)
    drv = build_navigator(s).driver
    assert drv.budget_guard.max_cost_usd == 1.25
    assert sorted(model for model, _, _ in built) == ["claude-fable-5-1", "claude-sonnet-5-5",
                                                      "claude-sonnet-5-5"]
    for _, timeout_s, kw in built:
        assert timeout_s == 77.0
        assert kw["fallbacks"] == ("claude-opus-5-5", "claude-sonnet-5-5")
        assert isinstance(kw["fallbacks"], tuple)
        assert kw["cooldown_s"] == 900
    dicts = [kw["cooldowns"] for _, _, kw in built]
    assert isinstance(dicts[0], dict) and all(d is dicts[0] for d in dicts)

def test_repos_file_builds_a_fleet_with_per_repo_runners(tmp_path):
    from navigator.config import RepoConfig
    from navigator.runtime.fleet import Fleet
    a, b = _seed_repo(tmp_path/"a"), _seed_repo(tmp_path/"b")
    s = _settings(tmp_path, a, repos=[RepoConfig(path=str(a)),
                                      RepoConfig(path=str(b), workdir="backend",
                                                 allowed_commands=["git status", "uv run --extra dev pytest"])])
    fleet = build_navigator(s)
    assert isinstance(fleet, Fleet)
    na, nb = fleet.navigators
    assert (na.repo, nb.repo) == (str(a), str(b))
    assert nb.workdir == "backend" and nb.runner.allowlist == ["git status", "uv run --extra dev pytest"]
    assert na.runner.allowlist == s.allowed_commands
    assert na.act is nb.act and na.driver is nb.driver  # one ACT, one model driver

def test_cycle_reports_quota_exhausted_without_the_sdk_text(tmp_path, monkeypatch):
    # N16: a quota-limited planner tick and a deferred build are reported by class name
    # and reset time only; the SDK text (which may carry a login URL) never reaches stdout
    import navigator.__main__ as m
    nav = build_navigator(_settings(tmp_path, _seed_repo(tmp_path)))
    iso = "2026-10-20T11:20:00+00:00"
    def broken_tick(base_commit_sha=None):
        raise QuotaExhaustedError("You've hit your session limit https://claude.ai/login?token=SECRET",
                                  marker="session limit", resets_at=iso)
    def fake_poll():
        nav.quota_deferral = {"error": "QuotaExhaustedError", "resets_at": iso}
        nav.quota_paused_until = datetime.fromisoformat(iso)
        return []
    monkeypatch.setattr(nav, "periodic_tick", broken_tick)
    monkeypatch.setattr(nav, "approval_poll", fake_poll)
    monkeypatch.setattr(nav, "merge_poll", lambda: [])
    summary = m._cycle(nav, nav.settings)
    assert summary["planner_error"] == f"QuotaExhaustedError: resets_at={iso}"
    assert summary["quota_exhausted"] == {"error": "QuotaExhaustedError", "resets_at": iso}
    assert summary["quota_paused_until"] == iso
    assert "error" not in summary
    blob = json.dumps(summary)
    assert "SECRET" not in blob and "session limit" not in blob and "claude.ai" not in blob

def test_builder_tools_flag_reaches_the_daemon_driver(tmp_path, monkeypatch):
    # operator decision 2026-10-28: NAVIGATOR_BUILDER_TOOLS gives the builder (and only the
    # builder) read-only tools with its own turn cap and timeout
    import navigator.__main__ as m
    from navigator.contracts.enums import Role
    built = []
    def fake_make(*, model, timeout_s, **kw):
        built.append((model, timeout_s, kw))
        return lambda p: model
    monkeypatch.setattr(m, "make_sdk_query_fn", fake_make)
    s = _settings(tmp_path, _seed_repo(tmp_path), driver="claude_sdk", sdk_model="claude-fable-5-1",
                  sdk_role_models={"planner": "claude-sonnet-5-5", "verifier": "claude-sonnet-5-5"},
                  builder_tools=True, sdk_builder_max_turns=20, sdk_builder_timeout_s=1200.0,
                  sdk_timeout_s=77.0)
    drv = build_navigator(s).driver
    assert drv.role_query_fns[Role.builder]("x") == "claude-fable-5-1"
    tooled = [(mo, t, kw) for mo, t, kw in built if "tools" in kw]
    assert len(tooled) == 1
    assert tooled[0][2]["tools"] == ("Read", "Grep", "Glob")
    assert tooled[0][2]["max_turns"] == 20
    assert tooled[0][1] == 1200.0
    others = [(mo, t, kw) for mo, t, kw in built if "tools" not in kw]
    assert len(others) == 3
    for _, t, kw in others:
        assert "max_turns" not in kw and t == 77.0

def test_builder_tests_flag_reaches_the_daemon_driver(tmp_path, monkeypatch):
    # build 2: NAVIGATOR_BUILDER_TESTS / _TEST_RUNS reach the driver; the flag needs builder_tools
    import navigator.__main__ as m
    monkeypatch.setattr(m, "make_sdk_query_fn",
                        lambda *, model, timeout_s, **kw: (lambda p, **k: model))
    repo = _seed_repo(tmp_path)
    s = _settings(tmp_path, repo, driver="claude_sdk", sdk_model="claude-fable-5-1",
                  builder_tools=True, builder_tests=True, builder_test_runs=4)
    drv = build_navigator(s).driver
    assert drv.builder_tests is True
    assert drv.builder_test_runs == 4
    s2 = _settings(tmp_path, repo, driver="claude_sdk", sdk_model="claude-fable-5-1",
                   builder_tools=True)
    drv2 = build_navigator(s2).driver
    assert drv2.builder_tests is False and drv2.builder_test_runs == 5
    s3 = _settings(tmp_path, repo, driver="claude_sdk", sdk_model="claude-fable-5-1",
                   builder_tests=True)
    with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_TESTS"):
        build_navigator(s3)

def test_runner_timeout_is_capped_at_300s_only_while_builder_tests_are_on(tmp_path, monkeypatch):
    # build 2b: the clamp exists only for the run_tests worker-thread overrun (asyncio.run waits
    # at most 300s for the executor), so it applies only while the tool is enabled; with it off
    # the verifier's full-suite bound is the configured NAVIGATOR_COMMAND_TIMEOUT_S (navigator's
    # own suite takes ~260s; other repos are unmeasured)
    _daemon_env(tmp_path, monkeypatch)
    monkeypatch.setenv("NAVIGATOR_COMMAND_TIMEOUT_S", "900")
    for k in ("NAVIGATOR_BUILDER_TOOLS", "NAVIGATOR_BUILDER_TESTS"):
        monkeypatch.delenv(k, raising=False)
    assert build_navigator(Settings.from_env()).runner.timeout_s == 900.0
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "1")
    assert build_navigator(Settings.from_env()).runner.timeout_s == 900.0  # tools alone never clamp
    monkeypatch.setenv("NAVIGATOR_BUILDER_TESTS", "1")
    assert build_navigator(Settings.from_env()).runner.timeout_s == 300.0
    monkeypatch.setenv("NAVIGATOR_COMMAND_TIMEOUT_S", "120")
    assert build_navigator(Settings.from_env()).runner.timeout_s == 120.0
    monkeypatch.setenv("NAVIGATOR_COMMAND_TIMEOUT_S", "300")
    assert build_navigator(Settings.from_env()).runner.timeout_s == 300.0
    # the same through Settings kwargs: the flag, not the driver, decides
    repo = _seed_repo(tmp_path / "kw")
    assert build_navigator(_settings(tmp_path, repo, command_timeout_s=900.0)).runner.timeout_s == 900.0
    assert build_navigator(_settings(tmp_path, repo, command_timeout_s=900.0, builder_tools=True,
                                     builder_tests=True)).runner.timeout_s == 300.0
