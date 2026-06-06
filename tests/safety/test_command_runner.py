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

def test_git_no_index_rejected(tmp_path):
    # git diff --no-index ignores the worktree and reads arbitrary absolute paths
    r = CommandRunner(allowlist=["git diff"], worktree_root=tmp_path)
    with pytest.raises(CommandRejected):
        r.run(["git","diff","--no-index","/etc/hosts","/dev/null"], cwd=tmp_path)

def test_absolute_path_token_rejected(tmp_path):
    r = CommandRunner(allowlist=["git diff"], worktree_root=tmp_path)
    with pytest.raises(CommandRejected):
        r.run(["git","diff","/etc/hosts"], cwd=tmp_path)

def test_missing_binary_wrapped_as_command_rejected(tmp_path):
    # passes allowlist/metachar/cwd checks, but the binary does not exist
    r = CommandRunner(allowlist=["definitelynotarealbinary"], worktree_root=tmp_path)
    with pytest.raises(CommandRejected):
        r.run(["definitelynotarealbinary"], cwd=tmp_path)

def test_timeout_wrapped_as_command_rejected(tmp_path):
    r = CommandRunner(allowlist=["sleep"], worktree_root=tmp_path, timeout_s=0.2)
    with pytest.raises(CommandRejected):
        r.run(["sleep","5"], cwd=tmp_path)
