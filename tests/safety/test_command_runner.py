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

# --- git flag allowlisting (v1 tightening) ---

def _runner(tmp_path):
    from navigator.safety.command_runner import CommandRunner
    return CommandRunner(allowlist=["git status", "git diff", "git log"],
                         worktree_root=tmp_path)

def test_git_safe_flags_allowed(tmp_path):
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    r = _runner(tmp_path)
    assert r.run(["git", "status", "--porcelain"], cwd=tmp_path).returncode == 0
    assert r.run(["git", "diff", "--stat"], cwd=tmp_path).returncode == 0
    assert r.run(["git", "log", "--oneline"], cwd=tmp_path).returncode in (0, 128)  # 128: empty repo

def test_git_file_writing_flag_rejected(tmp_path):
    import pytest
    from navigator.safety.errors import CommandRejected
    with pytest.raises(CommandRejected, match="--output"):
        _runner(tmp_path).run(["git", "log", "--output=x"], cwd=tmp_path)

def test_git_unknown_flag_rejected(tmp_path):
    import pytest
    from navigator.safety.errors import CommandRejected
    with pytest.raises(CommandRejected, match="--all"):
        _runner(tmp_path).run(["git", "log", "--all"], cwd=tmp_path)
