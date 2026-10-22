import os
import subprocess
import sys
from pathlib import Path
import pytest
from navigator.safety.command_runner import CommandRunner, project_python
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

def test_git_pathspec_separator_allowed(tmp_path):
    # observed live 2026-10-09: the first execute-mode verifier issued
    # `git diff -- <file>`; "--" only ends option parsing, so it is safe
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    r = _runner(tmp_path)
    assert r.run(["git", "diff", "--stat", "--", "README.md"], cwd=tmp_path).returncode == 0
    assert r.run(["git", "status", "--porcelain", "--", "README.md"], cwd=tmp_path).returncode == 0


def test_git_diff_bare_pathspec_separator_still_passes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    r = _runner(tmp_path)
    assert r.run(["git", "diff", "--", "README.md"], cwd=tmp_path).returncode == 0


def test_git_flag_rejection_names_subcommand(tmp_path):
    # outcome proposal-01M4HDFFTAEVBQ0M7WE6EAH55X only said "git flag not on safe
    # list: '--'" -- the subcommand must be named so the lesson is actionable
    r = CommandRunner(allowlist=["git show"], worktree_root=tmp_path)
    with pytest.raises(CommandRejected) as exc:
        r.run(["git", "show", "--", "README.md"], cwd=tmp_path)
    msg = str(exc.value)
    assert "'git show'" in msg
    assert "'--'" in msg
    assert "allowed: none" in msg


def test_git_flag_rejection_lists_allowed_flags(tmp_path):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "diff", "--all"], cwd=tmp_path)
    msg = str(exc.value)
    assert "'git diff'" in msg
    assert "'--all'" in msg
    assert "allowed: --, --cached, --name-only, --name-status, --staged, --stat" in msg


def test_commands_do_not_inherit_navigator_configuration(tmp_path, monkeypatch):
    # live 2026-10-10: verifier pytest inherited NAVIGATOR_DRIVER=claude_sdk from the
    # daemon, so env-reading tests in the target repo built a live SDK driver
    monkeypatch.setenv("NAVIGATOR_DRIVER", "claude_sdk")
    monkeypatch.setenv("NAVIGATOR_NOTIFY_ENV_FILE", "/secret/.env")
    seen = {}
    real_run = subprocess.run
    def spy(argv, **kw):
        seen["env"] = kw.get("env"); return real_run(argv, **kw)
    monkeypatch.setattr("navigator.safety.command_runner.subprocess.run", spy)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    _runner(tmp_path).run(["git", "status"], cwd=tmp_path)
    assert seen["env"] is not None
    assert not any(k.startswith("NAVIGATOR_") for k in seen["env"])
    assert "PATH" in seen["env"]


# --- N6: bounded git log -n / --max-count ---

def _repo_with_commits(tmp_path, n=2):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    for i in range(n):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                        "--allow-empty", "-m", f"c{i}"], cwd=tmp_path, check=True,
                       capture_output=True)


@pytest.mark.parametrize("args", [
    ["-n", "5"], ["-n5"], ["--max-count", "5"], ["--max-count=5"],
    ["-n", "1"], ["-n", "50"], ["--oneline", "-n", "3"], ["-n", "2", "--stat"],
    ["-1"],
])
def test_git_log_count_forms_accepted(tmp_path, args):
    _repo_with_commits(tmp_path)
    r = _runner(tmp_path).run(["git", "log", *args], cwd=tmp_path)
    # rc 0 also proves the integer reached git as the flag's value, not a revision
    assert r.returncode == 0, r.stderr


def test_git_log_count_limits_output(tmp_path):
    _repo_with_commits(tmp_path, n=3)
    r = _runner(tmp_path).run(["git", "log", "--oneline", "-n", "2"], cwd=tmp_path)
    assert len(r.stdout.strip().splitlines()) == 2


@pytest.mark.parametrize("args", [
    ["-n", "abc"], ["-n", "0"], ["-n", "999"], ["-n", "51"], ["-n"],
    ["-n0"], ["-n999"], ["-nabc"], ["-n", "-1"], ["--max-count"],
    ["--max-count=0"], ["--max-count=51"], ["--max-count="], ["--max-count", "x"],
])
def test_git_log_bad_count_rejected(tmp_path, args):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "log", *args], cwd=tmp_path)
    msg = str(exc.value)
    assert msg.startswith("git flag not on safe list for 'git log'")
    assert "1-50" in msg


def test_count_flag_only_for_git_log(tmp_path):
    with pytest.raises(CommandRejected, match="'git diff'"):
        _runner(tmp_path).run(["git", "diff", "-n", "5"], cwd=tmp_path)


def test_count_does_not_unlock_other_flags(tmp_path):
    with pytest.raises(CommandRejected, match="--all"):
        _runner(tmp_path).run(["git", "log", "-n", "5", "--all"], cwd=tmp_path)


def test_git_log_rejection_lists_count_flags(tmp_path):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "log", "--all"], cwd=tmp_path)
    assert "-n <1-50>" in str(exc.value)
    assert "--max-count <1-50>" in str(exc.value)


def test_git_flag_allowlist_text_covers_every_subcommand():
    from navigator.safety.command_runner import git_flag_allowlist_text
    text = git_flag_allowlist_text()
    assert "git status: " in text and "git diff: " in text and "git log: " in text
    assert "-n <1-50>" in text and "--max-count <1-50>" in text
    assert "--oneline" in text and "--porcelain" in text


# --- N6b: count parsing never crashes; dangerous flags after a count; global options ---

@pytest.mark.parametrize("args", [
    ["-n5", "-p"],
    ["-n", "5", "--output=x"],
    ["-n", "--exec"],
    ["--max-count=5=6"],
    ["-n", "\uff15"],
    ["-n", "\u0665"],
    ["-nn5"],
    ["-1n5"],
    ["-n", "9" * 5000],
    ["-n" + "9" * 5000],
    ["--max-count=" + "9" * 5000],
    ["--max-count", "9" * 5000],
])
def test_git_log_hostile_count_forms_rejected(tmp_path, args):
    # must raise CommandRejected (never ValueError) -- phases only catch CommandRejected
    with pytest.raises(CommandRejected):
        _runner(tmp_path).run(["git", "log", *args], cwd=tmp_path)


def test_huge_count_error_message_is_bounded(tmp_path):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "log", "-n", "9" * 5000], cwd=tmp_path)
    assert len(str(exc.value)) < 1000


@pytest.mark.parametrize("raw", [
    None, "", "0", "51", "100", "9" * 5000, "\uff15", "\u0665", "5=6", "n5", "-1",
])
def test_count_ok_returns_false_never_raises(raw):
    from navigator.safety.command_runner import _count_ok
    assert _count_ok(raw) is False


@pytest.mark.parametrize("raw", ["1", "5", "05", "50"])
def test_count_ok_accepts_valid(raw):
    from navigator.safety.command_runner import _count_ok
    assert _count_ok(raw) is True


@pytest.mark.parametrize("argv", [
    ["git", "-c", "core.fsmonitor=x", "status"],
    ["git", "--git-dir=x", "log"],
    ["git", "--exec-path=x", "status"],
    ["git", "-C", "x", "status"],
])
def test_git_global_options_rejected_even_with_bare_git_allowlisted(tmp_path, argv):
    r = CommandRunner(allowlist=["git"], worktree_root=tmp_path)
    with pytest.raises(CommandRejected, match="global option"):
        r.run(argv, cwd=tmp_path)


# --- N14: git status --untracked-files=no / -uno (exact tokens only) ---

def test_git_status_untracked_files_no_accepted(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "junk.txt").write_text("untracked\n")
    r = _runner(tmp_path)
    # control: without the flag the untracked file is listed
    ctl = r.run(["git", "status", "--porcelain"], cwd=tmp_path)
    assert ctl.returncode == 0, ctl.stderr
    assert "junk.txt" in ctl.stdout
    res = r.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "junk.txt" not in res.stdout  # the value reached git
    res2 = r.run(["git", "status", "--porcelain", "-uno"], cwd=tmp_path)
    assert res2.returncode == 0, res2.stderr
    assert "junk.txt" not in res2.stdout


@pytest.mark.parametrize("token", [
    "--untracked-files=all", "--untracked-files=normal", "--untracked-files=",
    "--untracked-files", "-u", "-uall", "-unormal", "-u=no",
])
def test_git_status_untracked_files_other_values_rejected(tmp_path, token):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "status", "--porcelain", token], cwd=tmp_path)
    msg = str(exc.value)
    assert msg.startswith("git flag not on safe list for 'git status'")
    assert repr(token) in msg


def test_git_status_two_token_untracked_form_rejected(tmp_path):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "status", "--untracked-files", "no"], cwd=tmp_path)
    assert "'--untracked-files'" in str(exc.value)


def test_untracked_flag_only_for_git_status(tmp_path):
    with pytest.raises(CommandRejected, match="'git diff'"):
        _runner(tmp_path).run(["git", "diff", "--untracked-files=no"], cwd=tmp_path)
    with pytest.raises(CommandRejected, match="'git log'"):
        _runner(tmp_path).run(["git", "log", "-uno"], cwd=tmp_path)
    _repo_with_commits(tmp_path)
    assert _runner(tmp_path).run(["git", "log", "--oneline"], cwd=tmp_path).returncode == 0


def test_git_status_rejection_and_allowlist_text_list_untracked_flags(tmp_path):
    with pytest.raises(CommandRejected) as exc:
        _runner(tmp_path).run(["git", "status", "--all"], cwd=tmp_path)
    msg = str(exc.value)
    assert "--untracked-files=no" in msg and "-uno" in msg and "'--all'" in msg
    from navigator.safety.command_runner import git_flag_allowlist_text
    text = git_flag_allowlist_text()
    assert "git status: " in text and "--untracked-files=no" in text and "-uno" in text
    diff_line = text.split("git diff: ")[1].split(";")[0]
    assert "--untracked-files=no" not in diff_line


# --- N15: a bare python/python3 runs under the project interpreter, never PATH ---

_SHOW_EXE = "print(__import__('sys').executable)"


def _decoy_python3(tmp_path, monkeypatch):
    """A fake python3 first on PATH: a PATH lookup of 'python3' would run it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    decoy = bindir / "python3"
    decoy.write_text("#!/bin/sh\necho DECOY\nexit 1\n")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return decoy


def test_bare_python3_runs_sys_executable_not_the_path_python3(tmp_path, monkeypatch):
    # live: the daemon's PATH put Apple's /usr/bin/python3 first, so py_compile
    # evidence was about the wrong interpreter
    _decoy_python3(tmp_path, monkeypatch)
    r = CommandRunner(allowlist=["python3 -c"], worktree_root=tmp_path).run(
        ["python3", "-c", _SHOW_EXE], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == sys.executable
    assert "DECOY" not in r.stdout + r.stderr
    assert r.interpreter == sys.executable
    # negative control: a plain PATH lookup WOULD have picked the decoy
    ctl = subprocess.run(["python3", "-c", _SHOW_EXE], env={"PATH": os.environ["PATH"]},
                         capture_output=True, text=True)
    assert ctl.stdout.strip() == "DECOY"


def test_py_compile_runs_under_the_project_interpreter(tmp_path, monkeypatch):
    _decoy_python3(tmp_path, monkeypatch)
    (tmp_path / "a.py").write_text("type Alias = int\n")  # valid only on Python >= 3.12
    r = CommandRunner(allowlist=["python3 -m py_compile"], worktree_root=tmp_path).run(
        ["python3", "-m", "py_compile", "a.py"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "DECOY" not in r.stdout + r.stderr
    assert (tmp_path / "__pycache__").is_dir()
    assert r.interpreter == sys.executable


def test_run_dir_venv_python_is_preferred_and_scoped(tmp_path):
    venv_py = tmp_path / "repo" / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\necho VENV \"$@\"\n")
    venv_py.chmod(0o755)
    (tmp_path / "repo" / "backend").mkdir()
    (tmp_path / "other").mkdir()
    runner = CommandRunner(allowlist=["python3 -m py_compile"], worktree_root=tmp_path)
    r = runner.run(["python3", "-m", "py_compile", "a.py"], cwd=tmp_path / "repo" / "backend")
    assert r.stdout.strip() == "VENV -m py_compile a.py"
    assert r.interpreter == str(venv_py)
    # a sibling directory's venv is never used
    (tmp_path / "other" / "a.py").write_text("x = 1\n")
    r2 = runner.run(["python3", "-m", "py_compile", "a.py"], cwd=tmp_path / "other")
    assert r2.returncode == 0, r2.stderr
    assert r2.interpreter == sys.executable
    assert "VENV" not in r2.stdout
    assert project_python(tmp_path / "repo" / "backend", tmp_path) == str(venv_py)
    assert project_python(tmp_path / "other", tmp_path) == sys.executable


def test_interpreter_substitution_does_not_widen_the_allowlist(tmp_path):
    runner = CommandRunner(allowlist=["python3 -m py_compile"], worktree_root=tmp_path)
    for argv in (["python", "-m", "py_compile", "a.py"],
                 [sys.executable, "-m", "py_compile", "a.py"],
                 [".venv/bin/python3", "-m", "py_compile", "a.py"],
                 ["python3", "-m", "pytest"],
                 ["python3", "-c", "print(1)"]):
        with pytest.raises(CommandRejected):
            runner.run(argv, cwd=tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    r = CommandRunner(allowlist=["git status"], worktree_root=tmp_path).run(
        ["git", "status"], cwd=tmp_path)
    assert r.returncode == 0
    assert r.interpreter is None
