"""Builder repair turns: run_build_and_verify(repair_turns=N) re-asks the builder in the
SAME worktree after a repairable verify failure, and on a repair turn every gate judges
the cumulative change since the run base (not just that turn's writes)."""
import subprocess

import pytest

from navigator.config import DEFAULT_ALLOWLIST, Settings
from navigator.contracts.enums import Role
from navigator.runtime import phases
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.phases import run_build_and_verify
from navigator.runtime.worktree import create_worktree
from navigator.safety.command_runner import CommandRunner

BAD = "def f(:\n"                     # py_compile fails on this
GOOD = "def f():\n    return 1\n"
GOOD2 = "def f():\n    return 2\n"
SKIP = "import pytest\npytest.skip('x')\n"
TEST_OK = "def test_ok():\n    assert True\n"
TEST_OK2 = "def test_ok():\n    assert 1\n"
PY_COMPILE = ["python3", "-m", "py_compile", "mod.py"]
AFFECTED_WITH_TESTS = ["mod.py", "tests/test_ok.py", "README.md"]


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _seed(tmp_path, files=None):
    """git repo at tmp_path/'repo' with one 'base' commit holding `files` (default: none,
    so there is no tests/ dir and the full suite never runs). Returns (repo, sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel, text in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if files:
        _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


class _Driver:
    """Scripted builder: one list of (relpath, content) per call, in order. The verifier
    always answers with `commands` (default: py_compile mod.py)."""

    def __init__(self, replies, commands=None):
        self.replies = [list(r) for r in replies]
        self.commands = [PY_COMPILE] if commands is None else commands
        self.requests = []

    def run_role(self, request):
        self.requests.append(request)
        if request.role == Role.builder:
            assert self.replies, "builder called more times than scripted"
            return DriverResult(role=Role.builder, file_writes=self.replies.pop(0))
        return DriverResult(role=Role.verifier, commands=[list(c) for c in self.commands])

    def builder_requests(self):
        return [r for r in self.requests if r.role == Role.builder]


def _run(tmp_path, driver, repair_turns, affected=("mod.py",), seed_files=None):
    repo, sha = _seed(tmp_path, seed_files)
    handle = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-r",
                             worktree_root=tmp_path / "wt")
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt")
    proposal = {"title": "t", "rationale": "r", "intended_changes": "edit mod.py",
                "affected_files": list(affected)}
    res = run_build_and_verify(driver=driver, worktree=handle, run_id="run-r", runner=runner,
                               proposal=proposal, repair_turns=repair_turns)
    return res, handle


def _patch_full_suite(monkeypatch, rc, output, repairable):
    """Make the verifier's py_compile count as 'runs tests' and replace the full-suite
    run with a recorder that sets the given outcome. Returns the recorder's call list."""
    calls = []

    def fake(runner, run_dir, res, breakers):
        calls.append(run_dir)
        res.verify_returncode, res.verify_output, res.repairable = rc, output, repairable

    monkeypatch.setattr(phases, "_runs_tests", lambda commands: True)
    monkeypatch.setattr(phases, "_run_full_suite", fake)
    return calls


def test_repair_off_is_todays_behaviour(tmp_path):
    d = _Driver([[("mod.py", BAD)]])
    res, _ = _run(tmp_path, d, repair_turns=0)
    assert res.verify_returncode != 0
    assert len(d.builder_requests()) == 1
    assert res.repair_turns_used == 0
    assert res.repair_failures == []
    assert res.repairable is True  # recorded even when no repair turn is granted


def test_repair_fixes_failure(tmp_path):
    d = _Driver([[("mod.py", BAD)], [("mod.py", GOOD)]])
    res, _ = _run(tmp_path, d, repair_turns=1)
    assert res.verify_returncode == 0
    assert res.repair_turns_used == 1
    reqs = d.builder_requests()
    assert len(reqs) == 2
    assert "previous_attempt_failure" not in reqs[0].context["proposal"]
    failure = reqs[1].context["proposal"]["previous_attempt_failure"]
    assert failure.startswith("repair turn 1 of 1")
    assert "mod.py" in failure
    # the builder sees its own first attempt, not the base text
    assert reqs[1].context["files"]["mod.py"] == BAD
    assert len(res.repair_failures) == 1 and "mod.py" in res.repair_failures[0]


def test_repair_capped(tmp_path):
    d = _Driver([[("mod.py", BAD)], [("mod.py", BAD)]])
    res, _ = _run(tmp_path, d, repair_turns=1)
    assert res.verify_returncode != 0
    assert len(d.builder_requests()) == 2
    assert res.repair_turns_used == 1
    assert len(res.repair_failures) == 1


def test_repair_skip_gate_judged_against_base(tmp_path):
    # the repair turn's write is compared with the BASE commit (file absent there), not
    # with the first attempt's text on disk, where the skip is already present
    d = _Driver([[("tests/test_x.py", SKIP)], [("tests/test_x.py", SKIP)]])
    res, _ = _run(tmp_path, d, repair_turns=1, seed_files={"tests/.keep": ""})
    assert res.verify_returncode != 0
    assert "builder added test skips" in res.verify_output
    assert "tests/test_x.py" in res.verify_output
    assert len(d.builder_requests()) == 2
    assert res.repair_turns_used == 1


def test_no_change_is_not_repaired(tmp_path):
    d = _Driver([[]])
    res, _ = _run(tmp_path, d, repair_turns=1)
    assert res.no_change is True
    assert res.verify_returncode != 0
    assert res.repairable is False
    assert res.repair_turns_used == 0
    assert len(d.builder_requests()) == 1


def test_rejected_command_is_not_repaired(tmp_path):
    d = _Driver([[("mod.py", GOOD)]], commands=[["rm", "-rf", "x"]])
    res, _ = _run(tmp_path, d, repair_turns=1)
    assert res.verify_returncode == 126
    assert res.repairable is False
    assert res.repair_turns_used == 0
    assert len(d.builder_requests()) == 1


def test_no_verifier_commands_is_not_repaired(tmp_path):
    d = _Driver([[("mod.py", GOOD)]], commands=[])
    res, _ = _run(tmp_path, d, repair_turns=1)
    assert res.verify_returncode == 1
    assert res.verify_output == "verifier produced no commands"
    assert res.repairable is False
    assert len(d.builder_requests()) == 1


def test_settings_builder_repair_turns(monkeypatch):
    monkeypatch.delenv("NAVIGATOR_BUILDER_REPAIR_TURNS", raising=False)
    assert Settings().builder_repair_turns == 1
    assert Settings.from_env().builder_repair_turns == 1
    monkeypatch.setenv("NAVIGATOR_BUILDER_REPAIR_TURNS", "0")
    assert Settings.from_env().builder_repair_turns == 0
    monkeypatch.setenv("NAVIGATOR_BUILDER_REPAIR_TURNS", "-1")
    with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_REPAIR_TURNS must be >= 0"):
        Settings.from_env()


def test_checkpoints_accumulate(tmp_path):
    # a builder may list a path more than once (the last write wins); every listed write
    # gets its own pre-write checkpoint, so two per attempt here and four in total
    d = _Driver([[("mod.py", BAD), ("mod.py", BAD + "# again\n")],
                 [("mod.py", GOOD), ("mod.py", GOOD2)]])
    res, handle = _run(tmp_path, d, repair_turns=1)
    assert res.verify_returncode == 0
    assert res.repair_turns_used == 1
    assert len(res.checkpoints) >= 4
    assert len(set(res.checkpoints)) == len(res.checkpoints)
    # first attempt's checkpoints, then the second's: chronological in the worktree log
    history = _git(handle.path, "rev-list", "--reverse", "HEAD").split()
    assert [c for c in history if c in res.checkpoints] == res.checkpoints
    assert res.file_writes == ["mod.py"]
    assert (handle.path / "mod.py").read_text() == GOOD2


def test_repair_rechecks_earlier_skips(tmp_path):
    # the repair turn does not touch tests/test_x.py; the skip the first attempt added
    # must still fail the run (cumulative skip gate against the base commit)
    d = _Driver([[("mod.py", GOOD), ("tests/test_x.py", SKIP)], [("mod.py", GOOD2)]])
    res, _ = _run(tmp_path, d, repair_turns=1, seed_files={"tests/.keep": ""})
    assert res.verify_returncode != 0
    assert "builder added test skips" in res.verify_output
    assert "tests/test_x.py" in res.verify_output
    assert len(d.builder_requests()) == 2
    assert res.repair_turns_used == 1


def test_repair_full_suite_covers_earlier_py_change(tmp_path, monkeypatch):
    calls = _patch_full_suite(monkeypatch, 1, "FAILED tests/test_ok.py::test_ok", True)
    d = _Driver([[("mod.py", GOOD), ("tests/test_ok.py", TEST_OK2)],
                 [("README.md", "x\n")]])
    res, _ = _run(tmp_path, d, repair_turns=1, affected=AFFECTED_WITH_TESTS,
                  seed_files={"tests/test_ok.py": TEST_OK})
    # the repair turn wrote only a non-.py file, yet the earlier .py change still
    # requires the full suite: the recorder ran once per attempt
    assert len(calls) == 2
    assert res.verify_returncode != 0
    assert len(d.builder_requests()) == 2
    assert res.repair_turns_used == 1
    assert res.file_writes == ["mod.py", "tests/test_ok.py", "README.md"]


def test_repair_no_false_missing_tests(tmp_path, monkeypatch):
    calls = _patch_full_suite(monkeypatch, 0, "1 passed", False)
    d = _Driver([[("mod.py", BAD), ("tests/test_ok.py", TEST_OK2)], [("mod.py", GOOD)]])
    res, _ = _run(tmp_path, d, repair_turns=1, affected=AFFECTED_WITH_TESTS,
                  seed_files={"tests/test_ok.py": TEST_OK})
    # attempt 1 fails at the verifier's py_compile; the repair turn writes only mod.py but
    # the earlier attempt's test change counts, so no 'made no test changes' failure
    assert res.verify_returncode == 0
    assert "made no test changes" not in res.verify_output
    assert len(calls) == 1
    assert len(res.repair_failures) == 1
    assert res.repair_turns_used == 1
    assert len(d.builder_requests()) == 2
