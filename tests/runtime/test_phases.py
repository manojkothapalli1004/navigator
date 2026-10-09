import subprocess
from pathlib import Path
import pytest
from navigator.runtime.worktree import create_worktree
from navigator.runtime.phases import run_build_and_verify, _resolve_write_target
from navigator.runtime.drivers.base import DriverRequest, DriverResult
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import DEFAULT_ALLOWLIST
from navigator.contracts.enums import Role


def test_resolve_write_target_accepts_relative_file(tmp_path):
    wt = tmp_path
    assert _resolve_write_target(wt, "navigator_touch.py") == (wt / "navigator_touch.py").resolve()
    assert _resolve_write_target(wt, "sub/file.py") == (wt / "sub/file.py").resolve()

@pytest.mark.parametrize("bad", [".", "", "..", "../escape.py", "/etc/passwd"])
def test_resolve_write_target_rejects_escape_and_root(tmp_path, bad):
    with pytest.raises(ValueError):
        _resolve_write_target(tmp_path, bad)


class _EmptyVerifierDriver:
    """Builder writes a file; verifier emits NO commands."""
    def run_role(self, request: DriverRequest) -> DriverResult:
        if request.role == Role.builder:
            return DriverResult(role=Role.builder,
                                file_writes=[("navigator_touch.py", "# x\n")])
        return DriverResult(role=Role.verifier, commands=[])

def test_zero_verify_commands_is_a_failure(tmp_path):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-z", worktree_root=tmp_path/"wt")
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")
    result = run_build_and_verify(driver=_EmptyVerifierDriver(), worktree=h, run_id="run-z", runner=runner)
    assert result.verify_returncode != 0

def _seed(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha

def test_build_and_verify_writes_and_runs(tmp_path):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-1", worktree_root=tmp_path/"wt")
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")
    result = run_build_and_verify(driver=FakeSubagentDriver(), worktree=h, run_id="run-1", runner=runner)
    assert (Path(h.path)/"navigator_touch.py").exists()
    assert result.verify_returncode == 0
    assert len(result.checkpoints) >= 1


class _RecordingDriver:
    def __init__(self):
        self.requests = {}
    def run_role(self, request):
        self.requests[request.role] = request
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=[("a.py", "x = 2\n")])
        return DriverResult(role=Role.verifier, commands=[["python3", "-m", "py_compile", "a.py"]])

def test_builder_and_verifier_receive_proposal_and_file_contents(tmp_path):
    # observed 2026-10-09: both roles got context={} — a real LLM with tools
    # disabled would have to invent whole files blind
    repo, _ = _seed(tmp_path)
    (repo/"a.py").write_text("x = 1\n")
    subprocess.run(["git","add","a.py"], cwd=repo, check=True)
    subprocess.run(["git","commit","-m","a"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-c", worktree_root=tmp_path/"wt")
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")
    drv = _RecordingDriver()
    proposal = {"title": "bump x", "rationale": "r", "intended_changes": "set x to 2",
                "affected_files": ["a.py", "new_file.py"]}
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-c", runner=runner, proposal=proposal)
    assert res.verify_returncode == 0
    b = drv.requests[Role.builder].context
    assert b["proposal"]["intended_changes"] == "set x to 2"
    assert b["files"] == {"a.py": "x = 1\n", "new_file.py": None}
    v = drv.requests[Role.verifier].context
    assert v["changed_files"] == ["a.py"]
    assert "pytest" in v["allowed_commands"]


class _ScriptedRoles:
    def __init__(self, writes, commands):
        self.writes, self.commands = writes, commands
    def run_role(self, request):
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=self.writes)
        return DriverResult(role=Role.verifier, commands=self.commands)

def _wt(tmp_path, run_id):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id=run_id, worktree_root=tmp_path/"wt")
    return h, CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")

def test_rejected_verifier_command_is_a_verify_failure_not_a_crash(tmp_path):
    # observed live 2026-10-09: a rejected verifier command raised out of the
    # phase and the run died as an "exception" instead of a verification result
    h, runner = _wt(tmp_path, "run-r")
    drv = _ScriptedRoles([("a.py", "x = 1\n")], [["rm", "-rf", "a.py"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-r", runner=runner,
                               proposal={"affected_files": ["a.py"]})
    assert res.verify_returncode != 0
    assert "rejected" in res.verify_output
    assert (Path(h.path)/"a.py").exists()

def test_builder_cannot_write_files_outside_the_approved_proposal(tmp_path):
    # the operator approved a proposal naming specific files; the builder may not widen it
    h, runner = _wt(tmp_path, "run-s")
    drv = _ScriptedRoles([("a.py", "x = 1\n"), ("sneaky.py", "boom\n")], [["git", "status"]])
    with pytest.raises(ValueError, match="sneaky.py"):
        run_build_and_verify(driver=drv, worktree=h, run_id="run-s", runner=runner,
                             proposal={"affected_files": ["a.py"]})
    assert not (Path(h.path)/"sneaky.py").exists()

def test_failed_test_count_trips_breaker(tmp_path):
    from navigator.safety.circuit_breakers import CircuitBreakers
    from navigator.safety.command_runner import CommandResult
    from navigator.safety.errors import BreakerTripped
    h, _ = _wt(tmp_path, "run-t")
    class _Runner:
        allowlist = ["pytest"]
        def run(self, argv, cwd):
            return CommandResult(1, "=== 7 failed, 3 passed in 0.1s ===", "")
    drv = _ScriptedRoles([("a.py", "x = 1\n")], [["pytest"]])
    cb = CircuitBreakers(max_tool_failures=99, max_identical_failures=99,
                         max_run_duration_s=999, max_failed_tests=5)
    with pytest.raises(BreakerTripped, match="max_failed_tests"):
        run_build_and_verify(driver=drv, worktree=h, run_id="run-t", runner=_Runner(),
                             proposal={"affected_files": ["a.py"]}, breakers=cb)
