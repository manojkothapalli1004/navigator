"""Settings bounds rev1 (U2a2): every int hot field has an upper bound, float hot fields must
be finite and bounded, and run_daemon's sleep is clamped so no settings document can stop the
daemon (time.sleep(10**10) raises OverflowError) or disable a timeout (Infinity)."""
from types import SimpleNamespace

import pytest

import navigator.__main__ as m
from navigator.__main__ import _sleep_seconds, build_navigator, run_daemon
from navigator.config import _HOT_FLOAT_MAX, _HOT_INT_RANGE, Settings
from tests.test_main import _cycle_lines, _seed_repo, _settings


@pytest.mark.parametrize("key,bounds", sorted(_HOT_INT_RANGE.items()))
def test_int_upper_bounds(key, bounds):
    lo, hi = bounds
    base = Settings()
    new, rejected = base.overlay({key: hi + 1})
    assert getattr(new, key) == getattr(base, key)
    assert rejected == [f"{key}: must be an integer {lo}..{hi}"]
    new, rejected = base.overlay({key: hi})
    assert rejected == []
    assert getattr(new, key) == hi


def test_tick_interval_bounds():
    base = Settings()
    new, rejected = base.overlay({"tick_interval_s": 86400})
    assert rejected == [] and new.tick_interval_s == 86400
    for bad in (86401, 10**10):
        new, rejected = base.overlay({"tick_interval_s": bad})
        assert new.tick_interval_s == 900
        assert rejected == ["tick_interval_s: must be an integer 60..86400"]


@pytest.mark.parametrize("key", ["sdk_timeout_s", "sdk_builder_timeout_s",
                                 "planner_throttle_hours", "planner_min_merge_rate"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_infinite_and_nan_floats_refused(key, value):
    base = Settings()
    new, rejected = base.overlay({key: value})
    assert getattr(new, key) == getattr(base, key)
    assert len(rejected) == 1 and rejected[0].startswith(f"{key}: ")


def test_float_upper_bounds():
    base = Settings()
    new, rejected = base.overlay({"sdk_timeout_s": 7201})
    assert new.sdk_timeout_s == base.sdk_timeout_s
    assert rejected == ["sdk_timeout_s: must be a number > 0 and <= 7200"]
    new, rejected = base.overlay({"sdk_timeout_s": 7200})
    assert rejected == [] and new.sdk_timeout_s == 7200.0
    new, rejected = base.overlay({"planner_throttle_hours": 721})
    assert new.planner_throttle_hours == base.planner_throttle_hours
    assert rejected == ["planner_throttle_hours: must be a number > 0 and <= 720"]
    new, rejected = base.overlay({"sdk_builder_timeout_s": 7200.0})
    assert rejected == [] and new.sdk_builder_timeout_s == 7200.0


def test_every_float_hot_field_has_a_bound():
    assert set(_HOT_FLOAT_MAX) == {"planner_throttle_hours", "sdk_timeout_s",
                                   "sdk_builder_timeout_s"}


def test_sleep_seconds_clamps():
    assert _sleep_seconds(Settings(tick_interval_s=10**10)) == 86400
    assert _sleep_seconds(Settings(tick_interval_s=0)) == 1
    assert _sleep_seconds(SimpleNamespace(tick_interval_s="x")) == 900
    assert _sleep_seconds(SimpleNamespace(tick_interval_s=True)) == 900
    assert _sleep_seconds(SimpleNamespace(tick_interval_s=float("nan"))) == 900
    assert _sleep_seconds(SimpleNamespace(tick_interval_s=float("inf"))) == 86400
    assert _sleep_seconds(SimpleNamespace(tick_interval_s=float("-inf"))) == 1
    assert _sleep_seconds(SimpleNamespace(tick_interval_s=10**400)) == 86400
    assert _sleep_seconds(SimpleNamespace()) == 900  # no attribute at all: never raises
    assert _sleep_seconds(Settings()) == 900
    assert _sleep_seconds(Settings(tick_interval_s=120)) == 120


def test_huge_int_float_field_refused_not_raised():
    base = Settings()
    new, rejected = base.overlay({"sdk_timeout_s": 10**400})
    assert new.sdk_timeout_s == base.sdk_timeout_s
    assert rejected == ["sdk_timeout_s: must be a number > 0 and <= 7200"]
    new, rejected = base.overlay({"planner_min_merge_rate": 10**400})
    assert new.planner_min_merge_rate == base.planner_min_merge_rate
    assert rejected == ["planner_min_merge_rate: must be a number between 0 and 1"]


def test_values_inside_the_ranges_still_apply():
    # NEGATIVE: the U2a behaviour for in-range values is unchanged
    new, rejected = Settings().overlay({"tick_interval_s": 120, "sdk_timeout_s": 5,
                                        "planner_throttle_hours": 24.5,
                                        "planner_min_merge_rate": 0.5, "builder_repair_turns": 5})
    assert rejected == []
    assert new.tick_interval_s == 120 and new.sdk_timeout_s == 5.0
    assert new.planner_throttle_hours == 24.5 and new.planner_min_merge_rate == 0.5
    assert new.builder_repair_turns == 5


def test_daemon_survives_huge_tick(tmp_path, monkeypatch, capsys):
    # NEGATIVE: run_daemon never raises, whatever tick_interval_s holds
    s = _settings(tmp_path, _seed_repo(tmp_path), tick_interval_s=10**10)
    nav = build_navigator(s)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if seconds > 86400:
            raise OverflowError("timestamp too large to convert to C PyTime_t")
    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    assert run_daemon(nav, s, max_cycles=2) is None
    lines = _cycle_lines(capsys.readouterr().out)
    assert [l["cycle"] for l in lines] == [1, 2]
    assert sleeps == [86400]
