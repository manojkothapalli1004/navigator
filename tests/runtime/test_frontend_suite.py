import subprocess
from pathlib import Path

import pytest

from navigator.config import DEFAULT_ALLOWLIST
from navigator.contracts.enums import Role
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.executor import _recorded_argvs, suite_green
from navigator.runtime.phases import reverify, run_build_and_verify
from navigator.runtime.worktree import create_worktree
from navigator.safety.command_runner import FRONTEND_SUITE, CommandResult, CommandRunner


class _Runner(CommandRunner):
    """Real allowlist runner for verifier commands; run_fixed is recorded, never spawned."""
    def __init__(self, root, rcs=None):
        super().__init__(allowlist=DEFAULT_ALLOWLIST, worktree_root=root)
        self.fixed, self.rcs = [], dict(rcs or {})
    def run_fixed(self, argv, cwd, timeout_s, env=None):
        self.fixed.append((tuple(argv), Path(cwd), timeout_s, env))
        return CommandResult(self.rcs.get(argv[0].rsplit("/", 1)[-1], 0), "out", "")


class _Driver:
    def __init__(self, writes):
        self.writes = writes
    def run_role(self, request):
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=self.writes)
        return DriverResult(role=Role.verifier, commands=[["git", "status"]])


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, live_modules=True):
    repo = tmp_path / "repo"; (repo / "frontend").mkdir(parents=True)
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "frontend/.gitignore").write_text("/node_modules\n")
    (repo / "frontend/page.tsx").write_text("export const x = 1;\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "base")
    if live_modules:
        (repo / "frontend/node_modules/.bin").mkdir(parents=True)
        (repo / "frontend/node_modules/.bin/vitest").write_text("#!/bin/sh\n")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha


FRONT_FIX = [("frontend/page.tsx", "export const x = 2;\n"),
             ("frontend/tests/page.test.tsx", "// t\n")]


def _build(tmp_path, writes=FRONT_FIX, rcs=None, live_modules=True, fps=None):
    repo, sha = _repo(tmp_path, live_modules)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-f", worktree_root=tmp_path / "wt")
    runner = _Runner(tmp_path / "wt", rcs)
    res = run_build_and_verify(driver=_Driver(writes), worktree=h, run_id="run-f", runner=runner,
                               frontend_dir="frontend", repo_path=repo, checker_fingerprints=fps)
    return res, runner, Path(h.path)


def test_frontend_suite_runs_in_order_after_verifier(tmp_path):
    res, runner, wt = _build(tmp_path)
    assert res.verify_returncode == 0, res.verify_output
    assert [f[0] for f in runner.fixed] == list(FRONTEND_SUITE)
    assert all(f[1] == (wt / "frontend").resolve() or f[1] == wt / "frontend" for f in runner.fixed)
    assert all(f[3]["HTTPS_PROXY"] == "http://127.0.0.1:9" for f in runner.fixed)
    assert [f[2] for f in runner.fixed] == [300, 300, 600]
    assert (wt / "frontend/node_modules/.bin/vitest").exists()
    assert res.verify_log[0]["argv_list"] == ["git", "status"] and "suite" not in res.verify_log[0]
    assert [e.get("suite") for e in res.verify_log[1:]] == ["frontend"] * 3


def test_frontend_suite_stops_at_first_failure(tmp_path):
    res, runner, _ = _build(tmp_path, rcs={"tsc": 2})
    assert len(runner.fixed) == 1
    assert res.verify_returncode == 2 and res.repairable is True


def test_frontend_suite_skipped_for_backend_only_change(tmp_path):
    res, runner, _ = _build(tmp_path, writes=[("backend/notes.md", "x\n")])
    assert runner.fixed == [] and res.verify_returncode == 0


def test_frontend_suite_missing_live_node_modules(tmp_path):
    res, runner, _ = _build(tmp_path, live_modules=False)
    assert runner.fixed == []
    assert res.verify_returncode == 1 and res.repairable is False
    assert "live checkout has no frontend/node_modules" in res.verify_output


def test_frontend_code_change_needs_test_change(tmp_path):
    res, runner, _ = _build(tmp_path, writes=[("frontend/page.tsx", "export const x = 3;\n")])
    assert res.verify_returncode == 1 and res.repairable is True
    assert "builder changed frontend code but made no test changes" in res.verify_output


def test_checker_run_exempt_from_test_change_rule(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: (0, "PASS"), raising=False)
    res, runner, _ = _build(tmp_path, writes=[("frontend/page.tsx", "export const x = 3;\n")], fps=["abc"])
    assert "no test changes" not in res.verify_output
    assert len(runner.fixed) == 3


def test_reverify_reruns_frontend_suite(tmp_path):
    repo, sha = _repo(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-r", worktree_root=tmp_path / "wt")
    wt = Path(h.path)
    recorded = [{"argv": "git status", "argv_list": ["git", "status"], "returncode": 0}] + [
        {"argv": " ".join(a), "argv_list": list(a), "returncode": 0, "suite": "frontend"} for a in FRONTEND_SUITE]
    argvs = _recorded_argvs(recorded)
    assert argvs == [["git", "status"]]
    runner = _Runner(tmp_path / "wt")
    res = reverify(runner, wt, argvs, changed_files=["frontend/page.tsx"], wt=wt,
                   frontend_dir="frontend", repo_path=repo)
    assert res.verify_returncode == 0, res.verify_output
    assert [f[0] for f in runner.fixed] == list(FRONTEND_SUITE)


def _suite(rc=0, skip=None):
    return [{"argv_list": list(a), "returncode": rc, "suite": "frontend"} for a in FRONTEND_SUITE if a != skip]


FRONT = ["frontend/components/X.tsx"]


def test_suite_green_requires_tagged_frontend_suite():
    assert suite_green(_suite(), FRONT, frontend_dir="frontend") is True
    assert suite_green(_suite(skip=FRONTEND_SUITE[2]), FRONT, frontend_dir="frontend") is False
    bad = _suite(); bad[1] = dict(bad[1], returncode=1); bad.append({"argv_list": ["git", "status"], "returncode": 0})
    assert suite_green(bad, FRONT, frontend_dir="frontend") is False


def test_suite_green_ignores_untagged_vitest_for_frontend_dir_repo():
    untagged = [{"argv_list": list(a), "returncode": 0} for a in FRONTEND_SUITE]
    assert suite_green(untagged, FRONT, frontend_dir="frontend") is False
    assert suite_green([{"argv_list": ["npx", "vitest", "run"], "returncode": 0}], FRONT,
                       frontend_dir="frontend") is False


def test_suite_green_frontend_dir_none_unchanged():
    assert suite_green([{"argv_list": ["npx", "vitest", "run"], "returncode": 0}], FRONT) is True


def test_checker_step_runs_after_frontend_suite(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    seen = []
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: seen.append((Path(source), fps)) or (0, "PASS"))
    res, runner, wt = _build(tmp_path, fps=["f1"])
    assert res.verify_returncode == 0, res.verify_output
    assert seen == [(wt / "frontend", ["f1"])]
    assert res.verify_log[-1]["suite"] == "checker" and res.verify_log[-1]["argv_list"] == ["checker-verify", "f1"]


def test_checker_step_fault_still_there_is_repairable(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: (1, "FAIL f1"))
    res, _, _ = _build(tmp_path, fps=["f1"])
    assert res.verify_returncode == 1 and res.repairable is True


def test_checker_step_no_fingerprints_fails(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: (0, ""))
    res, _, _ = _build(tmp_path, fps=[])
    assert res.verify_returncode == 1 and res.repairable is False
    assert "no checker faults recorded for this proposal" in res.verify_output


def test_checker_step_skipped_when_frontend_suite_fails(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    seen = []
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: seen.append(1) or (0, ""))
    res, _, _ = _build(tmp_path, rcs={"vitest": 1}, fps=["f1"])
    assert seen == [] and res.verify_returncode == 1


def test_frontend_suite_recclones_node_modules_every_time(tmp_path):
    # a builder write into the (gitignored) clone must not survive to the suite run
    repo, sha = _repo(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-n", worktree_root=tmp_path / "wt")
    wt = Path(h.path)
    tampered = wt / "frontend/node_modules/.bin/vitest"
    tampered.parent.mkdir(parents=True); tampered.write_text("#!/bin/sh\nexit 0\n# fake\n")
    runner = _Runner(tmp_path / "wt")
    res = run_build_and_verify(driver=_Driver(FRONT_FIX), worktree=h, run_id="run-n", runner=runner,
                               frontend_dir="frontend", repo_path=repo)
    assert res.verify_returncode == 0, res.verify_output
    assert tampered.read_text() == "#!/bin/sh\n"


class _CtxDriver(_Driver):
    def __init__(self, writes):
        super().__init__(writes); self.ctx = None
    def run_role(self, request):
        if request.role == Role.builder:
            self.ctx = request.context
        return super().run_role(request)


def _ctx_for(tmp_path, fps, affected):
    import navigator.runtime.phases as phases
    repo, sha = _repo(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-e", worktree_root=tmp_path / "wt")
    d = _CtxDriver([("frontend/page.tsx", "export const x = 4;\n")])
    run_build_and_verify(driver=d, worktree=h, run_id="run-e", runner=_Runner(tmp_path / "wt"),
                         proposal={"affected_files": affected}, frontend_dir="frontend",
                         repo_path=repo, checker_fingerprints=fps)
    return d.ctx


def test_checker_run_without_files_gets_editable_dir(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: (0, "PASS"), raising=False)
    assert _ctx_for(tmp_path, ["abc"], [])["editable_dir"] == "frontend"


def test_non_checker_or_listed_files_get_no_editable_dir(tmp_path, monkeypatch):
    import navigator.runtime.phases as phases
    monkeypatch.setattr(phases, "run_recheck", lambda source, fps: (0, "PASS"), raising=False)
    assert "editable_dir" not in _ctx_for(tmp_path / "a", None, [])
    assert "editable_dir" not in _ctx_for(tmp_path / "b", ["abc"], ["frontend/page.tsx"])
