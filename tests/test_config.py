import navigator.config as cfg

def test_defaults_are_fake_and_dry_run(monkeypatch):
    for k in ["NAVIGATOR_DRIVER","NAVIGATOR_MODE"]:
        monkeypatch.delenv(k, raising=False)
    s = cfg.Settings.from_env()
    assert s.driver == "fake"
    assert s.mode == "dry_run"
    assert "git status" in s.allowed_commands
    assert "uv run pytest" in s.allowed_commands

def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    assert cfg.Settings.from_env().mode == "execute"

def test_bad_tick_interval_gives_descriptive_error(monkeypatch):
    import pytest
    monkeypatch.setenv("NAVIGATOR_TICK_INTERVAL_S", "abc")
    with pytest.raises(ValueError, match="NAVIGATOR_TICK_INTERVAL_S"):
        cfg.Settings.from_env()

def test_from_env_wires_safety_and_daemon_fields(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MAX_COST_USD", "3.5")
    monkeypatch.setenv("NAVIGATOR_MAX_TOOL_FAILURES", "7")
    monkeypatch.setenv("NAVIGATOR_MAX_IDENTICAL_FAILURES", "2")
    monkeypatch.setenv("NAVIGATOR_MAX_RUN_DURATION_S", "900")
    monkeypatch.setenv("NAVIGATOR_MAX_PROPOSALS_PER_TICK", "1")
    monkeypatch.setenv("NAVIGATOR_MAX_FAILED_TESTS", "10")
    monkeypatch.setenv("NAVIGATOR_REPO", "/tmp/some-repo")
    monkeypatch.setenv("NAVIGATOR_LOCK_PATH", "/tmp/nav.lock")
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL", "claude-haiku-4-5-20251001")
    s = cfg.Settings.from_env()
    assert s.max_cost_usd == 3.5
    assert s.max_tool_failures == 7
    assert s.max_identical_failures == 2
    assert s.max_run_duration_s == 900
    assert s.max_proposals_per_tick == 1
    assert s.max_failed_tests == 10
    assert s.repo == "/tmp/some-repo"
    assert s.lock_path == "/tmp/nav.lock"
    assert s.sdk_model == "claude-haiku-4-5-20251001"

def test_sdk_timeout_env_wiring(monkeypatch):
    import pytest
    assert cfg.Settings.from_env().sdk_timeout_s == 300.0
    monkeypatch.setenv("NAVIGATOR_SDK_TIMEOUT_S", "120")
    assert cfg.Settings.from_env().sdk_timeout_s == 120.0
    monkeypatch.setenv("NAVIGATOR_SDK_TIMEOUT_S", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_TIMEOUT_S"):
        cfg.Settings.from_env()

def test_sdk_model_fallbacks_env_wiring(monkeypatch):
    # operator decision 2026-10-15: Fable -> Opus -> Sonnet when credits run out
    monkeypatch.delenv("NAVIGATOR_SDK_MODEL_FALLBACKS", raising=False)
    assert cfg.Settings().sdk_model_fallbacks == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert cfg.Settings.from_env().sdk_model_fallbacks == ["claude-opus-5-5", "claude-sonnet-5-5"]
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_FALLBACKS", " claude-opus-5-5 ,, claude-haiku-4-5 ")
    assert cfg.Settings.from_env().sdk_model_fallbacks == ["claude-opus-5-5", "claude-haiku-4-5"]
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_FALLBACKS", "")
    assert cfg.Settings.from_env().sdk_model_fallbacks == []  # empty disables fallback
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_FALLBACKS", " , ")
    assert cfg.Settings.from_env().sdk_model_fallbacks == []

def test_sdk_model_cooldown_env_wiring(monkeypatch):
    import pytest
    monkeypatch.delenv("NAVIGATOR_SDK_MODEL_COOLDOWN_S", raising=False)
    assert cfg.Settings().sdk_model_cooldown_s == 3600
    assert cfg.Settings.from_env().sdk_model_cooldown_s == 3600
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_COOLDOWN_S", "120")
    assert cfg.Settings.from_env().sdk_model_cooldown_s == 120
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_COOLDOWN_S", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_MODEL_COOLDOWN_S"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_COOLDOWN_S", "abc")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_MODEL_COOLDOWN_S"):
        cfg.Settings.from_env()

def test_from_env_bad_float_is_descriptive(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MAX_COST_USD", "not-a-number")
    import pytest
    with pytest.raises(ValueError, match="NAVIGATOR_MAX_COST_USD"):
        cfg.Settings.from_env()

def test_nonpositive_cost_and_failures_rejected(monkeypatch):
    import pytest
    monkeypatch.setenv("NAVIGATOR_MAX_COST_USD", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_MAX_COST_USD"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_MAX_COST_USD", "2.0")
    monkeypatch.setenv("NAVIGATOR_MAX_TOOL_FAILURES", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_MAX_TOOL_FAILURES"):
        cfg.Settings.from_env()

def test_allowed_commands_env_wiring(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_ALLOWED_COMMANDS", "git status, uv run pytest ,ruff check")
    s = cfg.Settings.from_env()
    assert s.allowed_commands == ["git status", "uv run pytest", "ruff check"]

def test_allowed_commands_unset_keeps_default(monkeypatch):
    monkeypatch.delenv("NAVIGATOR_ALLOWED_COMMANDS", raising=False)
    assert cfg.Settings.from_env().allowed_commands == cfg.DEFAULT_ALLOWLIST

def test_allowed_commands_rejects_dangerous_entries(monkeypatch):
    import pytest
    for bad in ("git status;rm", "cat /etc/passwd", "python -c `x`", "a|b"):
        monkeypatch.setenv("NAVIGATOR_ALLOWED_COMMANDS", bad)
        with pytest.raises(ValueError, match="NAVIGATOR_ALLOWED_COMMANDS"):
            cfg.Settings.from_env()

def test_command_timeout_is_env_wired_and_reaches_the_runner(tmp_path, monkeypatch):
    # a fresh worktree's `uv run pytest` builds a venv first: ~3 min live, past the 60s default
    monkeypatch.setenv("NAVIGATOR_COMMAND_TIMEOUT_S", "600")
    for k in ("NAVIGATOR_DRIVER", "NAVIGATOR_ACT_BASE_URL", "NAVIGATOR_ORC_BASE_URL",
              "NAVIGATOR_BUILDER_TOOLS", "NAVIGATOR_BUILDER_TESTS",
              "NAVIGATOR_REPOS_FILE"):
        monkeypatch.delenv(k, raising=False)  # hermetic: the daemon's env must not leak in
    s = cfg.Settings.from_env()
    assert s.command_timeout_s == 600
    from navigator.__main__ import build_navigator
    import subprocess
    repo = tmp_path/"r"; repo.mkdir(); subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    s.repo = str(repo); s.lock_path = str(tmp_path/"l"); s.worktree_root = str(tmp_path/"wt")
    # build 2b: the runner's per-command bound is clamped to 300s in build_navigator ONLY while
    # builder_tests is on (a run_tests worker must not outlive a cancelled builder query with
    # os.environ sealed); with the tool off the verifier keeps the configured bound
    assert build_navigator(s).runner.timeout_s == 600
    s.builder_tools = True; s.builder_tests = True
    assert build_navigator(s).runner.timeout_s == 300
    s.command_timeout_s = 120
    assert build_navigator(s).runner.timeout_s == 120

def test_per_role_sdk_models_from_env(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_PLANNER", "claude-sonnet-5-5")
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_VERIFIER", "claude-sonnet-5-5")
    monkeypatch.delenv("NAVIGATOR_SDK_MODEL_BUILDER", raising=False)
    assert cfg.Settings.from_env().sdk_role_models == {"planner": "claude-sonnet-5-5",
                                                        "verifier": "claude-sonnet-5-5"}

def test_repos_file_configures_multiple_repos(tmp_path, monkeypatch):
    import json
    f = tmp_path/"repos.json"
    f.write_text(json.dumps([
        {"path": "/r/navigator"},
        {"path": "/r/waker", "workdir": "backend",
         "allowed_commands": ["git status", "git diff", "uv run --extra dev pytest"]}]))
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(f))
    s = cfg.Settings.from_env()
    assert [r.path for r in s.repos] == ["/r/navigator", "/r/waker"]
    assert s.repos[1].workdir == "backend"
    assert s.repos[1].allowed_commands == ["git status", "git diff", "uv run --extra dev pytest"]
    assert s.repos[0].workdir == "." and s.repos[0].allowed_commands is None

def test_single_repo_env_still_works_without_repos_file(monkeypatch):
    monkeypatch.delenv("NAVIGATOR_REPOS_FILE", raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO", "/r/one")
    assert [r.path for r in cfg.Settings.from_env().repos] == ["/r/one"]

def test_repos_file_research_loop_fields(tmp_path, monkeypatch):
    import json
    f = tmp_path/"repos.json"
    f.write_text(json.dumps([{"path": "/r/trader", "brief": "one experiment",
                              "context_files": ["research_manager/morning_brief.md"],
                              "min_plan_interval_hours": 24, "max_proposals_per_tick": 1}]))
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(f))
    r = cfg.Settings.from_env().repos[0]
    assert (r.brief, r.context_files, r.min_plan_interval_hours, r.max_proposals_per_tick) == \
        ("one experiment", ["research_manager/morning_brief.md"], 24, 1)

def test_merge_in_dry_run_env_wiring(monkeypatch):
    # N8: operator opt-in to apply merge decisions while the daemon stays in dry_run
    import pytest
    monkeypatch.delenv("NAVIGATOR_MERGE_IN_DRY_RUN", raising=False)
    assert cfg.Settings().merge_in_dry_run is False
    assert cfg.Settings.from_env().merge_in_dry_run is False
    for v in ("1", "true", "True", " TRUE "):
        monkeypatch.setenv("NAVIGATOR_MERGE_IN_DRY_RUN", v)
        assert cfg.Settings.from_env().merge_in_dry_run is True, v
    for v in ("0", "false", "False"):
        monkeypatch.setenv("NAVIGATOR_MERGE_IN_DRY_RUN", v)
        assert cfg.Settings.from_env().merge_in_dry_run is False, v
    for v in ("yes", "2", "on"):
        monkeypatch.setenv("NAVIGATOR_MERGE_IN_DRY_RUN", v)
        with pytest.raises(ValueError, match="NAVIGATOR_MERGE_IN_DRY_RUN"):
            cfg.Settings.from_env()

def test_builder_tools_env_wiring(monkeypatch):
    # operator decision 2026-10-28: builder read-only tools, off by default
    import pytest
    for k in ("NAVIGATOR_BUILDER_TOOLS", "NAVIGATOR_SDK_BUILDER_MAX_TURNS",
              "NAVIGATOR_SDK_BUILDER_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.Settings().builder_tools is False
    assert cfg.Settings().sdk_builder_max_turns == 30
    assert cfg.Settings().sdk_builder_timeout_s == 900.0
    s = cfg.Settings.from_env()
    assert s.builder_tools is False
    assert s.sdk_builder_max_turns == 30
    assert s.sdk_builder_timeout_s == 900.0
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "1")
    assert cfg.Settings.from_env().builder_tools is True
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "true")
    assert cfg.Settings.from_env().builder_tools is True
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "0")
    assert cfg.Settings.from_env().builder_tools is False
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "maybe")
    with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_TOOLS"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_BUILDER_TOOLS", "0")
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_MAX_TURNS", "12")
    assert cfg.Settings.from_env().sdk_builder_max_turns == 12
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_MAX_TURNS", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_BUILDER_MAX_TURNS"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_MAX_TURNS", "abc")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_BUILDER_MAX_TURNS"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_MAX_TURNS", "12")
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_TIMEOUT_S", "12.5")
    assert cfg.Settings.from_env().sdk_builder_timeout_s == 12.5
    monkeypatch.setenv("NAVIGATOR_SDK_BUILDER_TIMEOUT_S", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_SDK_BUILDER_TIMEOUT_S"):
        cfg.Settings.from_env()

def test_builder_tests_env_wiring(monkeypatch):
    # build 2: the builder test runner rides on builder_tools; off by default, 5 runs per turn
    import pytest
    for k in ("NAVIGATOR_BUILDER_TESTS", "NAVIGATOR_BUILDER_TEST_RUNS"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.Settings().builder_tests is False
    assert cfg.Settings().builder_test_runs == 5
    s = cfg.Settings.from_env()
    assert s.builder_tests is False
    assert s.builder_test_runs == 5
    for v in ("1", "true"):
        monkeypatch.setenv("NAVIGATOR_BUILDER_TESTS", v)
        assert cfg.Settings.from_env().builder_tests is True, v
    monkeypatch.setenv("NAVIGATOR_BUILDER_TESTS", "0")
    assert cfg.Settings.from_env().builder_tests is False
    monkeypatch.setenv("NAVIGATOR_BUILDER_TESTS", "maybe")
    with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_TESTS"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_BUILDER_TESTS", "0")
    monkeypatch.setenv("NAVIGATOR_BUILDER_TEST_RUNS", "3")
    assert cfg.Settings.from_env().builder_test_runs == 3
    for bad in ("0", "abc"):
        monkeypatch.setenv("NAVIGATOR_BUILDER_TEST_RUNS", bad)
        with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_TEST_RUNS"):
            cfg.Settings.from_env()

def test_operator_preapprove_env_wiring(monkeypatch):
    # operator decision 2026-10-28 ('ask me less'): operator-filed proposals start approved; off by default
    import pytest
    monkeypatch.delenv("NAVIGATOR_OPERATOR_PREAPPROVE", raising=False)
    assert cfg.Settings().operator_preapprove is False
    assert cfg.Settings.from_env().operator_preapprove is False
    for v in ("1", "true", "True", " TRUE "):
        monkeypatch.setenv("NAVIGATOR_OPERATOR_PREAPPROVE", v)
        assert cfg.Settings.from_env().operator_preapprove is True, v
    for v in ("0", "false", "False"):
        monkeypatch.setenv("NAVIGATOR_OPERATOR_PREAPPROVE", v)
        assert cfg.Settings.from_env().operator_preapprove is False, v
    for v in ("yes", "2", "on"):
        monkeypatch.setenv("NAVIGATOR_OPERATOR_PREAPPROVE", v)
        with pytest.raises(ValueError, match="NAVIGATOR_OPERATOR_PREAPPROVE"):
            cfg.Settings.from_env()

def test_planner_throttle_env_wiring(monkeypatch):
    # planner throttle v1: gate the periodic planner on its own merge rate; off by default
    import pytest
    for k in ("NAVIGATOR_PLANNER_MIN_MERGE_RATE", "NAVIGATOR_PLANNER_MERGE_WINDOW",
              "NAVIGATOR_PLANNER_THROTTLE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.Settings().planner_min_merge_rate == 0.0
    assert cfg.Settings().planner_merge_window == 20
    assert cfg.Settings().planner_throttle_hours == 24.0
    s = cfg.Settings.from_env()
    assert s.planner_min_merge_rate == 0.0
    assert s.planner_merge_window == 20
    assert s.planner_throttle_hours == 24.0
    monkeypatch.setenv("NAVIGATOR_PLANNER_MIN_MERGE_RATE", "0.35")
    monkeypatch.setenv("NAVIGATOR_PLANNER_MERGE_WINDOW", "7")
    monkeypatch.setenv("NAVIGATOR_PLANNER_THROTTLE_HOURS", "12")
    s = cfg.Settings.from_env()
    assert s.planner_min_merge_rate == 0.35
    assert s.planner_merge_window == 7
    assert s.planner_throttle_hours == 12.0
    for bad in ("1.5", "-0.1"):
        monkeypatch.setenv("NAVIGATOR_PLANNER_MIN_MERGE_RATE", bad)
        with pytest.raises(ValueError, match="NAVIGATOR_PLANNER_MIN_MERGE_RATE"):
            cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_PLANNER_MIN_MERGE_RATE", "0.35")
    monkeypatch.setenv("NAVIGATOR_PLANNER_MERGE_WINDOW", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_PLANNER_MERGE_WINDOW"):
        cfg.Settings.from_env()
    monkeypatch.setenv("NAVIGATOR_PLANNER_MERGE_WINDOW", "7")
    monkeypatch.setenv("NAVIGATOR_PLANNER_THROTTLE_HOURS", "0")
    with pytest.raises(ValueError, match="NAVIGATOR_PLANNER_THROTTLE_HOURS"):
        cfg.Settings.from_env()

def test_repos_file_tier_fields(tmp_path, monkeypatch):
    # merge tiers 3a: per-repo tier_c_paths / tier_floor from NAVIGATOR_REPOS_FILE
    import json
    import pytest
    f = tmp_path/"repos.json"
    f.write_text(json.dumps([{"path": "/r/waker", "tier_c_paths": ["app/pay*"], "tier_floor": "B"},
                             {"path": "/r/vent"}]))
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(f))
    repos = cfg.Settings.from_env().repos
    assert repos[0] == cfg.RepoConfig(path="/r/waker", tier_c_paths=["app/pay*"], tier_floor="B")
    assert repos[0].tier_c_paths == ["app/pay*"] and repos[0].tier_floor == "B"
    assert repos[1].tier_c_paths is None and repos[1].tier_floor == "A"
    f.write_text(json.dumps([{"path": "/r/waker", "tier_floor": "D"}]))
    with pytest.raises(ValueError, match="tier_floor"):
        cfg.Settings.from_env()
    f.write_text(json.dumps([{"path": "/r/waker", "tier_c_paths": "x"}]))
    with pytest.raises(ValueError, match="tier_c_paths"):
        cfg.Settings.from_env()
    f.write_text(json.dumps([{"path": "/r/waker", "tier_c_paths": ["ok", 3]}]))
    with pytest.raises(ValueError, match="tier_c_paths"):
        cfg.Settings.from_env()

def test_merge_tier_toggles_env_wiring(monkeypatch):
    # merge tiers 3a: both toggles are parsed (off by default) and read by nothing yet
    import pytest
    for k in ("NAVIGATOR_MERGE_TIER_A_AUTO", "NAVIGATOR_MERGE_TIER_B_BATCH"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.Settings().merge_tier_a_auto is False
    assert cfg.Settings().merge_tier_b_batch is False
    s = cfg.Settings.from_env()
    assert s.merge_tier_a_auto is False and s.merge_tier_b_batch is False
    for name, attr in (("NAVIGATOR_MERGE_TIER_A_AUTO", "merge_tier_a_auto"),
                       ("NAVIGATOR_MERGE_TIER_B_BATCH", "merge_tier_b_batch")):
        for v in ("1", "true", "True", " TRUE "):
            monkeypatch.setenv(name, v)
            assert getattr(cfg.Settings.from_env(), attr) is True, (name, v)
        for v in ("0", "false", "False"):
            monkeypatch.setenv(name, v)
            assert getattr(cfg.Settings.from_env(), attr) is False, (name, v)
        for v in ("yes", "2", "on"):
            monkeypatch.setenv(name, v)
            with pytest.raises(ValueError, match=name):
                cfg.Settings.from_env()
        monkeypatch.delenv(name, raising=False)
