"""U2a settings hot reload: Settings.overlay applies the ACT document navigator/settings
(validated hot fields only) over the environment Settings; env-only fields are refused."""
import dataclasses

import pytest

from navigator.config import ENV_ONLY_FIELDS, HOT_FIELDS, WIRING_FIELDS, Settings


def test_overlay_applies_hot_fields():
    new, rejected = Settings().overlay({"sdk_role_models": {"builder": "claude-opus-5-5"},
                                        "merge_tier_a_auto": True, "digest_hour": 7})
    assert new.sdk_role_models == {"builder": "claude-opus-5-5"}
    assert new.merge_tier_a_auto is True
    assert new.digest_hour == 7
    assert rejected == []


@pytest.mark.parametrize("key,value", [("allowed_commands", ["rm"]), ("max_cost_usd", 99.0),
                                       ("worktree_root", "/somewhere/else")])
def test_overlay_rejects_env_only_field(key, value):
    base = Settings()
    new, rejected = base.overlay({key: value})
    assert getattr(new, key) == getattr(base, key)
    assert rejected == [f"{key}: env-only"]


def test_overlay_skips_invalid_key_keeps_others():
    new, rejected = Settings().overlay({"tick_interval_s": "fast", "mode": "execute"})
    assert new.mode == "execute"
    assert new.tick_interval_s == 900
    assert len(rejected) == 1 and rejected[0].startswith("tick_interval_s:")


@pytest.mark.parametrize("key,value", [
    ("tick_interval_s", 59),
    ("digest_hour", 24),
    ("builder_repair_turns", -1),
    ("planner_min_merge_rate", 1.5),
    ("merge_tier_a_auto", 1),          # int is not a bool
    ("max_open_proposals", True),      # bool is not an int
    ("mode", "live"),
    ("sdk_role_models", {"critic": "x"}),
    ("decisions_tz", "Mars/Base"),
])
def test_overlay_bounds(key, value):
    base = Settings()
    new, rejected = base.overlay({key: value})
    assert getattr(new, key) == getattr(base, key)
    assert len(rejected) == 1 and rejected[0].startswith(f"{key}: ")


def test_overlay_accepts_values_at_the_bounds():
    new, rejected = Settings().overlay({"tick_interval_s": 60, "digest_hour": 0,
                                        "builder_repair_turns": 0, "planner_min_merge_rate": 1,
                                        "sdk_timeout_s": 5, "decisions_tz": "Europe/London",
                                        "sdk_role_models": {}})
    assert rejected == []
    assert new.tick_interval_s == 60 and new.digest_hour == 0 and new.builder_repair_turns == 0
    assert new.planner_min_merge_rate == 1.0 and new.sdk_timeout_s == 5.0
    assert new.decisions_tz == "Europe/London" and new.sdk_role_models == {}


def test_overlay_unknown_key_rejected():
    base = Settings()
    new, rejected = base.overlay({"colour": "blue"})
    assert rejected == ["colour: unknown"]
    assert new == base


def test_overlay_rejections_are_in_sorted_key_order():
    _, rejected = Settings().overlay({"zeta": 1, "allowed_commands": [], "mode": "x"})
    assert rejected == ["allowed_commands: env-only", "mode: must be dry_run or execute",
                        "zeta: unknown"]


def test_overlay_empty_doc_is_identity():
    base = Settings()
    new, rejected = base.overlay({})
    assert new == base
    assert rejected == []


def test_overlay_non_dict():
    base = Settings()
    new, rejected = base.overlay([1])
    assert new is base
    assert rejected == ["<document>: not an object"]


def test_overlay_does_not_mutate_the_base():
    base = Settings()
    before = dataclasses.replace(base)
    base.overlay({"mode": "execute", "sdk_role_models": {"planner": "m"}})
    assert base == before


def test_field_sets_partition_settings():
    names = {f.name for f in dataclasses.fields(Settings)}
    assert HOT_FIELDS | ENV_ONLY_FIELDS == names
    assert HOT_FIELDS & ENV_ONLY_FIELDS == set()
    assert WIRING_FIELDS <= HOT_FIELDS


def test_new_decisions_fields_default_to_none_and_are_not_read_from_env():
    s = Settings()
    assert (s.digest_hour, s.decisions_nudges, s.decisions_tz) == (None, None, None)
    e = Settings.from_env()
    assert (e.digest_hour, e.decisions_nudges, e.decisions_tz) == (None, None, None)


def test_repo_memory_fields_are_env_only():
    # reviewer memory B1: the switch and the root are plumbing the document may not set
    assert {"repo_memory", "repo_memory_root"} <= ENV_ONLY_FIELDS
    new, rejected = Settings().overlay({"repo_memory": True, "repo_memory_root": "/x"})
    assert new == Settings()
    assert rejected == ["repo_memory: env-only", "repo_memory_root: env-only"]
