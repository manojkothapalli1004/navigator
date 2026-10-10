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
    for k in ("NAVIGATOR_DRIVER", "NAVIGATOR_ACT_BASE_URL", "NAVIGATOR_ORC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)  # hermetic: the daemon's env must not leak in
    s = cfg.Settings.from_env()
    assert s.command_timeout_s == 600
    from navigator.__main__ import build_navigator
    import subprocess
    repo = tmp_path/"r"; repo.mkdir(); subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    s.repo = str(repo); s.lock_path = str(tmp_path/"l"); s.worktree_root = str(tmp_path/"wt")
    assert build_navigator(s).runner.timeout_s == 600

def test_per_role_sdk_models_from_env(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_PLANNER", "claude-sonnet-5-5")
    monkeypatch.setenv("NAVIGATOR_SDK_MODEL_VERIFIER", "claude-sonnet-5-5")
    monkeypatch.delenv("NAVIGATOR_SDK_MODEL_BUILDER", raising=False)
    assert cfg.Settings.from_env().sdk_role_models == {"planner": "claude-sonnet-5-5",
                                                        "verifier": "claude-sonnet-5-5"}
