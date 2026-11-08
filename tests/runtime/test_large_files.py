"""Navigator large-file builds (spec 2026-11-08): digest + disk-resolved edits."""
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from navigator.runtime import phases
from navigator.runtime.drivers.base import DriverRequest
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.worktree import create_worktree
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import DriverParseError
from navigator.config import DEFAULT_ALLOWLIST
from navigator.contracts.enums import Role


def _big_py(n=1500):
    return "".join(f"def f_{i:04d}():\n    # pad {'x' * 60}\n    return {i}\n\n" for i in range(1, n + 1))


def _info(text):
    raw = text.encode()
    return {"chars": len(text), "lines": text.count("\n"), "sha256": hashlib.sha256(raw).hexdigest()}


def _driver(replies, prompts=None):
    it = iter(replies)
    last = replies[-1]   # a repair turn re-asks; the scripted builder repeats its last reply

    def q(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return json.dumps(next(it, last))
    return ClaudeSDKDriver(query_fn=q, budget_guard=BudgetGuard(10.0))


def _req(wt, files, large, **extra):
    ctx = {"files": files, "large_files": large, "proposal": {"affected_files": list(files)}, **extra}
    return DriverRequest(role=Role.builder, context=ctx, worktree_path=str(wt))


def _ctx_for(wt, rel, mention=""):
    files, large = phases._read_affected(wt, [rel], mention)
    return files, large


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for c in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "b", "--allow-empty"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha


def _wt_with(tmp_path, files):
    repo, sha = _repo(tmp_path, files)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-l", worktree_root=tmp_path / "wt")
    return h, Path(h.path)


# 1
def test_edit_near_end_of_150k_file_is_applied_to_full_disk_text(tmp_path):
    big = _big_py()
    assert len(big) >= 150_000
    h, wt = _wt_with(tmp_path, {"big.py": big})
    replies = [{"file_edits": [["big.py", "    return 1490\n", "    return 9999\n"]], "summary": "s"},
               {"commands": [["python3", "-m", "py_compile", "big.py"]], "summary": "v"}]
    drv = _driver(replies)
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt")
    proposal = {"title": "t", "rationale": "r", "intended_changes": "change f_1490", "affected_files": ["big.py"]}
    res = phases.run_build_and_verify(driver=drv, worktree=h, run_id="run-l", runner=runner, proposal=proposal)
    expected = big.replace("    return 1490\n", "    return 9999\n", 1)
    assert res.file_writes == ["big.py"]
    on_disk = (wt / "big.py").read_text()
    assert on_disk == expected
    assert len(on_disk) == len(big) + 0  # same-length replacement here
    # and the driver-level write pair is the full text
    (wt / "big.py").write_text(big)
    files, large = _ctx_for(wt, "big.py", "f_1490")
    out = _driver([replies[0]]).run_role(_req(wt, files, large))
    assert out.file_writes == [("big.py", expected)]


# 2
def test_old_text_must_be_unique_in_the_real_file_not_the_window(tmp_path):
    big = _big_py() + "def dup():\n    return 'UNIQ_IN_WINDOW'\n" + _big_py(10).replace("f_", "g_") \
        + "def dup2():\n    return 'UNIQ_IN_WINDOW'\n"
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "dup")
    assert "sha256" in large["big.py"] and large["big.py"]["chars"] == len(big)
    with pytest.raises(DriverParseError, match="occurs 2 times"):
        _driver([{"file_edits": [["big.py", "return 'UNIQ_IN_WINDOW'", "return 1"]],
                  "summary": "s"}]).run_role(_req(wt, files, large))


# 3
def test_large_file_changed_since_context_is_refused(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "f_1490")
    edit = {"file_edits": [["big.py", "    return 1490\n", "    return 1\n"]], "summary": "s"}
    (wt / "big.py").write_text(big + "# extra\n")           # size path
    with pytest.raises(DriverParseError, match="large"):
        _driver([edit]).run_role(_req(wt, files, large))


# 27
def test_size_same_length_content_change_refused(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "f_1490")
    changed = big.replace("return 7\n", "return 8\n", 1)
    assert len(changed) == len(big) and changed != big
    (wt / "big.py").write_text(changed)                       # sha path
    with pytest.raises(DriverParseError, match="large"):
        _driver([{"file_edits": [["big.py", "    return 1490\n", "    return 1\n"]],
                  "summary": "s"}]).run_role(_req(wt, files, large))


# 4
def test_file_writes_to_existing_large_file_refused(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "f_1490")
    for rel in ("big.py", "./big.py"):
        with pytest.raises(DriverParseError, match="large file the builder only saw in part; use file_edits"):
            _driver([{"file_writes": [[rel, "x = 1\n"]], "summary": "s"}]).run_role(_req(wt, files, large))


# 5
def test_digest_contains_symbol_window_for_named_function_and_outline(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "fix f_1490 please")
    d = files["big.py"]
    assert len(d) <= 40_000 + 100
    assert d.startswith("[LARGE FILE big.py:")
    assert "function f_1490" in d or "def f_1490" in d
    assert "L" in d and "def f_1490" in d
    assert "def f_1490():\n    # pad " in d             # verbatim, unprefixed
    assert large["big.py"]["chars"] == len(big)


# 6
def test_digest_falls_back_for_unparseable_py_and_non_py(tmp_path):
    bad = "def broken(:\n" + "".join(f"def g_{i:05d}():\n    return {i}  # {'y' * 40}\n" for i in range(2500))
    ts = "".join(f"function h_{i:05d}() {{ return {i}; }} // {'z' * 40}\n" for i in range(2500))
    assert len(bad) > 100_000 and len(ts) > 100_000
    _, wt = _wt_with(tmp_path, {"bad.py": bad, "x.ts": ts})
    files, large = phases._read_affected(wt, ["bad.py", "x.ts"], "g_02000 and h_02000")
    assert "def g_02000" in files["bad.py"] and "return 2000" in files["bad.py"]
    assert "function h_02000" in files["x.ts"] and "return 2000;" in files["x.ts"]
    assert set(large) == {"bad.py", "x.ts"}


# 7
def test_small_file_unchanged(tmp_path):
    _, wt = _wt_with(tmp_path, {"a.py": "x = 1\n"})
    files, large = phases._read_affected(wt, ["a.py", "new.py"], "")
    assert files == {"a.py": "x = 1\n", "new.py": None} and large == {}


# 8
def test_editable_dir_large_file(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"frontend/big.py": big})
    ctx = {"files": {}, "editable_dir": "frontend", "proposal": {"title": "f_1490", "affected_files": []}}
    res = _driver([{"file_edits": [["frontend/big.py", "    return 1490\n", "    return 5\n"]],
                    "summary": "s"}]).run_role(
        DriverRequest(role=Role.builder, context=ctx, worktree_path=str(wt)))
    assert res.file_writes == [("frontend/big.py", big.replace("    return 1490\n", "    return 5\n", 1))]


# 9
def test_scope_still_enforced_for_large_file(tmp_path):
    big = _big_py()
    h, wt = _wt_with(tmp_path, {"big.py": big, "ok.py": "x = 1\n"})
    drv = _driver([{"file_edits": [["big.py", "    return 1490\n", "    return 5\n"]], "summary": "s"}])
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt")
    # big.py is not in affected_files, so it is not shown/large; the edit is refused one way or another
    with pytest.raises((ValueError, DriverParseError)):
        phases.run_build_and_verify(driver=drv, worktree=h, run_id="run-l", runner=runner,
                                    proposal={"intended_changes": "x", "affected_files": ["ok.py"]})
    assert (wt / "big.py").read_text() == big


# 10
def test_failure_tail_symbol_gets_a_window(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    class Rec:
        ctx = None
        def run_role(self, request):
            if request.role == Role.builder:
                Rec.ctx = request.context
                from navigator.runtime.drivers.base import DriverResult
                return DriverResult(role=Role.builder, file_writes=[])
            from navigator.runtime.drivers.base import DriverResult
            return DriverResult(role=Role.verifier)
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt")
    h = type("H", (), {"path": str(wt)})()
    phases.run_build_and_verify(driver=Rec(), worktree=h, run_id="run-l", runner=runner,
                                proposal={"intended_changes": "x", "affected_files": ["big.py"],
                                          "previous_attempt_failure": "Traceback: in f_1333 raised"})
    assert "def f_1333" in Rec.ctx["files"]["big.py"] and "big.py" in Rec.ctx["large_files"]


# 28
def test_exactly_cap_sized_file_gets_digest(tmp_path):
    text = "x = 1\n" * 10_000
    assert len(text) == phases._MAX_FILE_CHARS
    _, wt = _wt_with(tmp_path, {"cap.py": text})
    files, large = phases._read_affected(wt, ["cap.py"], "")
    assert "cap.py" in large and files["cap.py"].startswith("[LARGE FILE")


# 29
def test_crlf_large_file_refused(tmp_path):
    big = _big_py().replace("\n", "\r\n")
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = phases._read_affected(wt, ["big.py"], "f_1490")
    assert "big.py" in large                                   # kept, apply refuses
    with pytest.raises(DriverParseError):
        _driver([{"file_edits": [["big.py", "    return 1490", "    return 1"]],
                  "summary": "s"}]).run_role(_req(wt, files, large))


# 30
def test_failed_second_edit_leaves_nothing(tmp_path):
    big = _big_py()
    _, wt = _wt_with(tmp_path, {"big.py": big})
    files, large = _ctx_for(wt, "big.py", "f_1490")
    from navigator.runtime.drivers.claude_sdk_driver import _EditApplyError
    with pytest.raises(_EditApplyError):
        _driver([{"file_edits": [["big.py", "    return 1490\n", "    return 1\n"],
                                 ["big.py", "NOT THERE", "x"]], "summary": "s"}]
                ).run_role(_req(wt, files, large))
    assert (wt / "big.py").read_text() == big


# 33
def test_total_digest_budget_across_files(tmp_path):
    texts = {f"m{i}.py": _big_py().replace("f_", f"m{i}_") for i in range(4)}
    _, wt = _wt_with(tmp_path, texts)
    files, large = phases._read_affected(wt, list(texts), "m0_1490 m1_1490 m2_1490 m3_1490")
    assert len(large) == 4
    assert sum(len(v) for v in files.values()) <= phases._LARGE_TOTAL_BUDGET + 2000
    assert phases._LARGE_TOTAL_BUDGET == 80_000
