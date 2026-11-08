"""Part C (spec 2026-11-08): edit an existing in-scope file the builder was not shown."""
import json
import os
import subprocess
from pathlib import Path
import pytest
from navigator.runtime import phases
from navigator.runtime.drivers import claude_sdk_driver as drv_mod
from navigator.runtime.drivers.base import DriverRequest
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver, _EditApplyError
from navigator.runtime.worktree import create_worktree
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import DriverParseError
from navigator.config import DEFAULT_ALLOWLIST
from navigator.contracts.enums import Role


def _run(wt, edits, affected=("pkg/mod.py",), files=None):
    d = ClaudeSDKDriver(query_fn=lambda p: json.dumps({"file_edits": edits, "summary": "s"}),
                        budget_guard=BudgetGuard(10.0))
    ctx = {"files": files if files is not None else {"pkg/mod.py": "x = 1\n"},
           "proposal": {"affected_files": list(affected)}}
    return d.run_role(DriverRequest(role=Role.builder, context=ctx, worktree_path=str(wt)))


def _wt(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_recipes.py").write_text("assert f() == 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_text("x = 1\n")
    (tmp_path / "pkg/other.py").write_text("y = 1\n")
    return tmp_path


def test_edit_to_unshown_existing_test_file_is_applied_from_disk(tmp_path):
    wt = _wt(tmp_path)
    res = _run(wt, [["tests/test_recipes.py", "assert f() == 1", "assert f() == 2"]])
    assert res.file_writes == [("tests/test_recipes.py", "assert f() == 2\n")]


def test_edit_to_unshown_test_file_through_phases_attempt(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "tests/test_recipes.py").write_text("assert f() == 1\n")
    (repo / "pkg/mod.py").write_text("x = 1\n")
    for c in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
              ["add", "-A"], ["commit", "-m", "b"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-u", worktree_root=tmp_path / "wt")
    replies = iter([{"file_edits": [["tests/test_recipes.py", "assert f() == 1", "assert f() == 2"]],
                     "summary": "s"},
                    {"commands": [["python3", "-m", "py_compile", "tests/test_recipes.py"]], "summary": "v"}])
    d = ClaudeSDKDriver(query_fn=lambda p: json.dumps(next(replies)), budget_guard=BudgetGuard(10.0))
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt")
    res = phases.run_build_and_verify(
        driver=d, worktree=h, run_id="run-u", runner=runner,
        proposal={"title": "t", "rationale": "r", "intended_changes": "c", "affected_files": ["pkg/mod.py"]})
    assert res.file_writes == ["tests/test_recipes.py"]
    assert (Path(h.path) / "tests/test_recipes.py").read_text() == "assert f() == 2\n"


def test_unshown_file_in_affected_files_is_loaded(tmp_path):
    wt = _wt(tmp_path)
    res = _run(wt, [["pkg/other.py", "y = 1", "y = 2"]], affected=("pkg/mod.py", "pkg/other.py"))
    assert res.file_writes == [("pkg/other.py", "y = 2\n")]


def test_unshown_non_test_file_outside_affected_files_still_fails(tmp_path):
    wt = _wt(tmp_path)
    with pytest.raises(_EditApplyError):
        _run(wt, [["pkg/other.py", "y = 1", "y = 2"]])
    assert (wt / "pkg/other.py").read_text() == "y = 1\n"


@pytest.mark.parametrize("old", ["assert f() == 99", "assert"])
def test_unshown_test_file_old_must_occur_exactly_once_on_disk(tmp_path, old):
    wt = _wt(tmp_path)
    (wt / "tests/test_recipes.py").write_text("assert f() == 1\nassert g() == 1\n")
    with pytest.raises(_EditApplyError, match="occurs"):
        _run(wt, [["tests/test_recipes.py", old, "x"]])


def test_unshown_refuses_symlink_escape_dir_missing_dotdot_absolute(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_o.py").write_text("SECRET = 1\n")
    wt = _wt(tmp_path / "w") if (tmp_path / "w").mkdir() is None else None
    (wt / "tests/test_link_out.py").symlink_to(outside / "test_o.py")
    (wt / "tests/test_link_in.py").symlink_to(wt / "tests/test_recipes.py")
    (wt / "tests/sub").mkdir()
    (wt / "tests/linkdir").symlink_to(outside, target_is_directory=True)
    for rel in ("tests/test_link_out.py", "tests/test_link_in.py", "tests/sub", "tests/test_missing.py",
                "../x.py", str(outside / "test_o.py"), "tests/linkdir/test_o.py", "tests/../pkg/mod.py"):
        with pytest.raises(_EditApplyError) as ei:
            _run(wt, [[rel, "SECRET", "x"]])
        assert "SECRET" not in str(ei.value)
    assert (outside / "test_o.py").read_text() == "SECRET = 1\n"


def test_hardlink_refused(tmp_path):
    outside = tmp_path / "o.py"
    outside.write_text("SECRET = 1\n")
    wt = tmp_path / "w"
    (wt / "tests").mkdir(parents=True)
    os.link(outside, wt / "tests/test_h.py")
    with pytest.raises(_EditApplyError):
        _run(wt, [["tests/test_h.py", "SECRET", "x"]])


def test_git_component_refused(tmp_path):
    wt = _wt(tmp_path)
    (wt / "tests/.git").mkdir()
    (wt / "tests/.git/test_x.py").write_text("a = 1\n")
    with pytest.raises(_EditApplyError):
        _run(wt, [["tests/.git/test_x.py", "a = 1", "a = 2"]])


def test_unshown_file_over_cap_is_refused(tmp_path):
    wt = _wt(tmp_path)
    (wt / "tests/test_recipes.py").write_text("assert f() == 1\n" + "# pad\n" * 12_000)
    with pytest.raises(_EditApplyError):
        _run(wt, [["tests/test_recipes.py", "assert f() == 1", "assert f() == 2"]])


def test_unshown_test_file_with_empty_affected_files(tmp_path):
    wt = _wt(tmp_path)
    res = _run(wt, [["tests/test_recipes.py", "== 1", "== 3"]], affected=())
    assert res.file_writes == [("tests/test_recipes.py", "assert f() == 3\n")]
    with pytest.raises(_EditApplyError):
        _run(wt, [["pkg/mod.py", "x = 1", "x = 2"]], affected=(), files={})


def test_unshown_loader_follows_phases_is_test_file(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    monkeypatch.setattr(drv_mod, "_is_test_file", lambda rel: False)
    with pytest.raises(_EditApplyError):
        _run(wt, [["tests/test_recipes.py", "== 1", "== 2"]])
    monkeypatch.setattr(drv_mod, "_is_test_file", lambda rel: rel == "pkg/other.py")
    assert _run(wt, [["pkg/other.py", "y = 1", "y = 2"]]).file_writes == [("pkg/other.py", "y = 2\n")]


def test_shown_file_is_still_resolved_from_context_not_disk(tmp_path):
    wt = _wt(tmp_path)
    res = _run(wt, [["pkg/mod.py", "x = 5", "x = 6"]], files={"pkg/mod.py": "x = 5\n"})
    assert res.file_writes == [("pkg/mod.py", "x = 6\n")]


def test_case_variant_double_key_refused(tmp_path):
    wt = _wt(tmp_path)
    with pytest.raises(_EditApplyError):
        _run(wt, [["tests/test_recipes.py", "== 1", "== 2"], ["Tests/test_recipes.py", "== 2", "== 3"]])
