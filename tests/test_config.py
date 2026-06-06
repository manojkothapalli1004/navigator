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
