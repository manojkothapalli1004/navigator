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
