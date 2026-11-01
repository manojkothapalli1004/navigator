import json

import pytest

from navigator.runtime.executor import suite_green
from navigator.runtime.merge_tiers import classify

ACT_PATHS = ("frontend/components/*", "frontend/app/*", "frontend/lib/*", "frontend/tests/*")
FRONT = ["frontend/components/cards/CombosTable.tsx", "frontend/tests/CombosTable.test.tsx"]


def cls(changed, origin, floor="B", paths=ACT_PATHS):
    return classify(changed, repo_name="agent-control-tower", tier_floor=floor, origin=origin, checker_a_paths=paths)


def test_checker_frontend_only_is_A_despite_floor_B():
    assert cls(FRONT, "site-checker") == "A"


def test_checker_with_backend_file_is_not_A():
    assert cls(FRONT + ["backend/act/adapters/bot.py"], "site-checker") == "B"


def test_checker_with_c_path_is_C():
    assert cls(FRONT + ["frontend/.env"], "site-checker") == "C"


def test_non_checker_origin_unchanged():
    assert cls(FRONT, "operator") == "B"
    assert cls(FRONT, None) == "B"


def test_empty_change_is_C():
    assert cls([], "site-checker") == "C"


def test_no_checker_paths_configured_means_no_override():
    assert cls(FRONT, "site-checker", paths=()) == "B"


def test_frontend_change_needs_a_vitest_run_to_count_as_green():
    ok = {"returncode": 0, "argv_list": ["npx", "vitest", "run"]}
    other = {"returncode": 0, "argv_list": ["git", "status"]}
    assert suite_green([ok], FRONT) is True
    assert suite_green([other], FRONT) is False
