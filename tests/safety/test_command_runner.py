import subprocess
from pathlib import Path
import pytest
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import CommandRejected

def _runner(tmp_path):
    return CommandRunner(allowlist=["git status","uv run pytest","python -m py_compile"],
                         worktree_root=tmp_path)

def test_allowlisted_command_runs(tmp_path):
    subprocess.run(["git","init"], cwd=tmp_path, check=True, capture_output=True)
    r = _runner(tmp_path).run(["git","status"], cwd=tmp_path)
    assert r.returncode == 0

def test_arbitrary_command_rejected(tmp_path):
    with pytest.raises(CommandRejected):
        _runner(tmp_path).run(["rm","-rf","/"], cwd=tmp_path)

def test_shell_metacharacters_rejected(tmp_path):
    with pytest.raises(CommandRejected):
        _runner(tmp_path).run(["git","status",";","whoami"], cwd=tmp_path)

def test_git_push_rejected(tmp_path):
    with pytest.raises(CommandRejected):
        _runner(tmp_path).run(["git","push"], cwd=tmp_path)

def test_cwd_outside_worktree_rejected(tmp_path):
    with pytest.raises(CommandRejected):
        _runner(tmp_path).run(["git","status"], cwd=Path("/etc"))
