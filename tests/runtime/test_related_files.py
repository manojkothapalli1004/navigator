import subprocess
from pathlib import Path
from navigator.runtime.phases import _related_files, run_build_and_verify
from navigator.runtime.worktree import create_worktree
from navigator.runtime.drivers.base import DriverResult
from navigator.safety.command_runner import CommandRunner
from navigator.config import DEFAULT_ALLOWLIST
from navigator.contracts.enums import Role

MARKER = "\n# [truncated by navigator]"


def _write(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return rel


def test_named_file_gets_its_own_allowance_and_is_truncated(tmp_path):
    # live 2026-10-12: test-only proposals never showed the builder the modules they named
    tracked = [
        _write(tmp_path, "pkg/huge.py", "y = 2\n" * 17000),      # ~102k, named
        _write(tmp_path, "small.py", "s = 1\n"),                  # unnamed, small
        _write(tmp_path, "other_big.py", "z = 3\n" * 17000),     # ~102k, unnamed
    ]
    out = _related_files(tmp_path, tracked, set(),
                         mention_text="add tests for pkg/huge.py behaviour")
    assert out["pkg/huge.py"].startswith("y = 2\n")
    assert out["pkg/huge.py"].endswith(MARKER)
    assert len(out["pkg/huge.py"]) <= 60_000 + len(MARKER)
    assert out["small.py"] == "s = 1\n"
    assert "other_big.py" not in out


def test_named_files_follow_order_of_first_mention_and_stop_after_truncation(tmp_path):
    tracked = [
        _write(tmp_path, "a_first.py", "a" * 40_000),
        _write(tmp_path, "b_second.py", "b" * 40_000),
        _write(tmp_path, "c_third.py", "c" * 1_000),
    ]
    out = _related_files(tmp_path, tracked, set(),
                         mention_text="see b_second.py then a_first.py then c_third.py")
    assert out["b_second.py"] == "b" * 40_000
    assert out["a_first.py"] == "a" * 20_000 + MARKER
    # named allowance stopped; c_third.py still arrives through the general allowance
    assert out["c_third.py"] == "c" * 1_000


def test_workdir_relative_mention_is_included(tmp_path):
    tracked = [_write(tmp_path, "backend/services/x.py", "q = 1\n" * 17000)]
    out = _related_files(tmp_path, tracked, set(),
                         mention_text="cover services/x.py with tests", workdir="backend")
    assert "backend/services/x.py" in out
    assert out["backend/services/x.py"].endswith(MARKER)


def test_bare_name_does_not_match_inside_a_longer_path(tmp_path):
    tracked = [_write(tmp_path, "x.py", "r = 1\n" * 17000)]
    out = _related_files(tmp_path, tracked, set(), mention_text="edit services/x.py")
    assert "x.py" not in out


def test_skipped_named_file_is_not_duplicated(tmp_path):
    tracked = [_write(tmp_path, "mod.py", "m = 1\n")]
    out = _related_files(tmp_path, tracked, {"mod.py"}, mention_text="change mod.py")
    assert "mod.py" not in out


def test_total_related_chars_are_bounded(tmp_path):
    tracked = [_write(tmp_path, "named.py", "n = 1\n" * 17000)]
    for i in range(40):
        tracked.append(_write(tmp_path, f"pkg/m{i:02d}.py", "k" * 3000))
    out = _related_files(tmp_path, tracked, set(), mention_text="named.py")
    assert "named.py" in out
    assert any(k.startswith("pkg/") for k in out)
    assert sum(len(v) for v in out.values()) <= 120_000 + len(MARKER)


class _RecordingDriver:
    def __init__(self):
        self.requests = {}
    def run_role(self, request):
        self.requests[request.role] = request
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=[("a.py", "x = 2\n")])
        return DriverResult(role=Role.verifier, commands=[["python3", "-m", "py_compile", "a.py"]])


def test_build_passes_intended_changes_and_rationale_as_mention_text(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    _write(repo, "a.py", "x = 1\n")
    _write(repo, "lib/mod.py", "v = 1\n" * 17000)
    _write(repo, "lib/why.py", "w = 1\n" * 17000)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "b"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-rf", worktree_root=tmp_path / "wt")
    drv = _RecordingDriver()
    run_build_and_verify(driver=drv, worktree=h, run_id="run-rf",
                         runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt"),
                         proposal={"affected_files": ["a.py"],
                                   "intended_changes": "use lib/mod.py from a.py",
                                   "rationale": "lib/why.py explains it"})
    ctx = drv.requests[Role.builder].context
    related = ctx["related_files"]
    assert related["lib/mod.py"].startswith("v = 1\n")
    assert len(related["lib/mod.py"]) <= 60_000 + len(MARKER)
    assert "a.py" not in related          # writable file stays only in context.files
    assert ctx["files"]["a.py"] == "x = 1\n"
