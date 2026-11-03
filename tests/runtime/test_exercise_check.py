"""2026-11-02: does a run's new test actually exercise its change? Its changed test files are run
against the BASE code (the builder's source edits left out); passing there means they don't."""
import subprocess
from pathlib import Path

from navigator.runtime import phases
from navigator.runtime.executor import unexercised_tests
from navigator.runtime.phases import PhaseResult, _run_exercise_check, exercise_verdict


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _wt(tmp_path):
    wt = tmp_path / "wt"; (wt / "tests").mkdir(parents=True); (wt / "pkg").mkdir()
    _git(wt, "init", "-q"); _git(wt, "config", "user.email", "t@t"); _git(wt, "config", "user.name", "t")
    (wt / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (wt / "tests" / "test_mod.py").write_text("from pkg.mod import f\n\ndef test_f():\n    assert f() == 1\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
    # the builder's edits, committed as a checkpoint BEFORE verification (as _attempt does)
    (wt / "pkg" / "mod.py").write_text("def f():\n    return 2\n")
    (wt / "tests" / "test_mod.py").write_text("from pkg.mod import f\n\ndef test_f():\n    assert f() == 2\n")
    (wt / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    (wt / "tests" / "conftest.py").write_text("")
    _git(wt, "add", "-A"); _git(wt, "commit", "-qm", "post-build checkpoint")
    BASE[wt] = base
    return wt


class Runner:
    allowlist = ["pytest"]

    def __init__(self, rc):
        self.rc, self.calls = rc, []

    def run(self, argv, cwd):
        cwd = Path(cwd)
        # what is on disk where pytest runs: base source + the run's tests
        self.calls.append((list(argv), cwd, (cwd / "pkg" / "mod.py").read_text(),
                           (cwd / "tests" / "test_new.py").exists()))
        return type("R", (), {"returncode": self.rc, "stdout": "1 failed" if self.rc else "2 passed", "stderr": ""})()


BASE: dict = {}
WRITES = ["pkg/mod.py", "tests/test_mod.py", "tests/test_new.py", "tests/conftest.py"]


def test_verdicts():
    assert exercise_verdict(1) is True and exercise_verdict(2) is True   # fails / errors on the old code
    assert exercise_verdict(0) is False and exercise_verdict(5) is False  # passes / nothing collected
    assert exercise_verdict(126) is None and exercise_verdict(3) is None  # can't tell


def test_runs_new_tests_against_base_source_and_cleans_up(tmp_path):
    wt = _wt(tmp_path); res = PhaseResult(); r = Runner(rc=1)
    _run_exercise_check(r, wt, wt, WRITES, res, base=BASE[wt])
    argv, cwd, src, has_new = r.calls[0]
    assert argv[0] == "pytest" and sorted(argv[-2:]) == ["tests/test_mod.py", "tests/test_new.py"]
    assert not any(a.endswith("conftest.py") for a in argv)  # copied, never passed as a test path
    assert src == "def f():\n    return 1\n" and has_new  # the BASE commit, not the checkpointed HEAD
    assert cwd != wt and not cwd.exists()  # a scratch worktree, removed afterwards
    e = res.verify_log[-1]
    assert e["suite"] == "exercise" and e["exercised"] is True and e["returncode"] == 1
    assert res.verify_returncode == 0  # informational: never fails verification by itself
    assert (wt / "pkg" / "mod.py").read_text() == "def f():\n    return 2\n"  # the run's own edits untouched


def test_tests_that_pass_on_base_are_not_exercised(tmp_path):
    wt = _wt(tmp_path); res = PhaseResult()
    _run_exercise_check(Runner(rc=0), wt, wt, WRITES, res, base=BASE[wt])
    assert res.verify_log[-1]["exercised"] is False


def test_only_when_source_and_tests_both_change(tmp_path):
    wt = _wt(tmp_path)
    for writes in (["pkg/mod.py"], ["tests/test_new.py"], ["README.md"]):
        res = PhaseResult(); r = Runner(rc=1)
        _run_exercise_check(r, wt, wt, writes, res, base=BASE[wt])
        assert r.calls == [] and res.verify_log == []


def test_runner_rejection_is_unknown_not_a_failure(tmp_path):
    from navigator.safety.errors import CommandRejected
    wt = _wt(tmp_path); res = PhaseResult()

    class Rejecting(Runner):
        def run(self, argv, cwd):
            raise CommandRejected("nope")
    _run_exercise_check(Rejecting(rc=0), wt, wt, WRITES, res, base=BASE[wt])
    assert res.verify_log[-1]["exercised"] is None and res.verify_returncode == 0


def test_run_dir_inside_worktree_is_kept(tmp_path):
    # repos with a workdir (e.g. backend/) run pytest from the same relative place in the scratch copy
    wt = _wt(tmp_path); res = PhaseResult(); r = Runner(rc=1)
    sub = wt / "pkg"
    r.run = lambda argv, cwd, _r=r: (_r.calls.append((argv, Path(cwd))),
                                     type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())[1]
    _run_exercise_check(r, wt, sub, ["pkg/mod.py", "pkg/test_mod2.py", "tests/test_new.py"], res, base=BASE[wt])
    assert r.calls and r.calls[0][1].name == "pkg" and r.calls[0][1] != sub


def test_auto_merge_blocker():
    assert unexercised_tests([{"suite": "exercise", "exercised": False}]) is True
    assert unexercised_tests([{"suite": "exercise", "exercised": True}]) is False
    assert unexercised_tests([{"suite": "exercise", "exercised": None}]) is False
    assert unexercised_tests([{"argv": "pytest -q", "returncode": 0}]) is False
    assert unexercised_tests([]) is False



def test_entry_goes_before_the_full_suite_entry(tmp_path):
    # suite_green reads the LAST entry's returncode: the exercise result (rc 1 when it works!) must not be last
    wt = _wt(tmp_path); res = PhaseResult()
    res.verify_log.append({"argv": "pytest -q", "returncode": 0})
    _run_exercise_check(Runner(rc=1), wt, wt, WRITES, res, base=BASE[wt])
    assert res.verify_log[-1] == {"argv": "pytest -q", "returncode": 0}
    assert res.verify_log[-2]["suite"] == "exercise"


def test_copy_error_is_unknown_not_a_crash(tmp_path, monkeypatch):
    wt = _wt(tmp_path); res = PhaseResult()
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(phases.shutil, "copyfile", boom)
    _run_exercise_check(Runner(rc=1), wt, wt, WRITES, res, base=BASE[wt])
    assert res.verify_log[-1]["exercised"] is None
    assert not (wt.parent / f"{wt.name}.exercise").exists()


def test_no_existing_test_file_means_no_run(tmp_path):
    wt = _wt(tmp_path); res = PhaseResult(); r = Runner(rc=0)
    _run_exercise_check(r, wt, wt, ["pkg/mod.py", "tests/test_gone.py"], res, base=BASE[wt])
    assert r.calls == [] and res.verify_log == []  # never the whole base suite by accident


def test_stale_registered_worktree_is_pruned(tmp_path):
    wt = _wt(tmp_path)
    scratch = wt.parent / f"{wt.name}.exercise"
    _git(wt, "worktree", "add", "--detach", "-q", str(scratch), BASE[wt])
    import shutil
    shutil.rmtree(scratch)  # crashed: git still has it registered, the dir is gone
    res = PhaseResult(); r = Runner(rc=1)
    _run_exercise_check(r, wt, wt, WRITES, res, base=BASE[wt])
    assert r.calls and res.verify_log[-1]["exercised"] is True


def test_rebase_keeps_the_exercise_result():
    from navigator.runtime.executor import carry_exercise
    old = [{"argv": "pytest -q", "returncode": 0}, {"suite": "exercise", "exercised": False, "returncode": 0}]
    new = [{"argv": "pytest -q", "returncode": 0}]
    out = carry_exercise(old, new)
    assert out[-1] == {"argv": "pytest -q", "returncode": 0} and out[0]["suite"] == "exercise"
    assert carry_exercise([{"argv": "x", "returncode": 0}], new) == new


# ---- terminal review 1 (2026-11-02): the wiring itself must be tested ----

def test_auto_merge_ok_refuses_a_run_whose_tests_were_not_exercised(monkeypatch):
    from types import SimpleNamespace
    from navigator.runtime import executor as ex
    for name in ("is_audited", "base_on_head"):
        monkeypatch.setattr(ex, name, lambda *a, **k: True)
    monkeypatch.setattr(ex, "changed_files_for_run", lambda repo, run: ["pkg/mod.py", "tests/test_mod.py"])
    monkeypatch.setattr(ex, "repo_config_for", lambda settings, repo: None)
    monkeypatch.setattr(ex, "classify_for_repo", lambda *a, **k: "A")
    monkeypatch.setattr(ex, "suite_green", lambda *a, **k: True)
    act = SimpleNamespace(get_proposal=lambda pid: SimpleNamespace(origin="periodic"))
    run = SimpleNamespace(proposal_id="p", verification=[{"suite": "exercise", "exercised": False, "returncode": 0}])
    assert ex.auto_merge_ok(act, run, "/repo", None) is False
    run.verification = [{"suite": "exercise", "exercised": True, "returncode": 1}]
    assert ex.auto_merge_ok(act, run, "/repo", None) is True


def test_attempt_hands_the_real_base_to_the_exercise_check(tmp_path, monkeypatch):
    from navigator.config import DEFAULT_ALLOWLIST
    from navigator.contracts.enums import Role
    from navigator.runtime.drivers.base import DriverResult
    from navigator.runtime.phases import run_build_and_verify
    from navigator.runtime.worktree import create_worktree
    from navigator.safety.command_runner import CommandResult, CommandRunner

    repo = tmp_path / "repo"; (repo / "tests").mkdir(parents=True); (repo / "pkg").mkdir()
    _git(repo, "init", "-q"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "pkg" / "mod.py").write_text("X = 1\n"); (repo / "tests" / "test_mod.py").write_text("def test_x():\n    pass\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    class R(CommandRunner):
        def run(self, argv, cwd, env=None):
            return CommandResult(0, "1 passed", "")  # no real pytest; the spy below is what matters

    class D:
        def run_role(self, req):
            if req.role == Role.builder:
                return DriverResult(role=Role.builder, file_writes=[("pkg/mod.py", "X = 2\n"),
                                                                     ("tests/test_mod.py", "def test_x():\n    assert 1\n")])
            return DriverResult(role=Role.verifier, commands=[["pytest", "-q"]])

    seen = {}
    monkeypatch.setattr(phases, "_run_exercise_check",
                        lambda runner, wt, run_dir, changed, res, base: seen.update(base=base, wt=wt))
    h = create_worktree(repo=repo, base_commit_sha=base, run_id="run-x", worktree_root=tmp_path / "wt")
    run_build_and_verify(driver=D(), worktree=h, run_id="run-x",
                         runner=R(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt"))
    assert seen, "the exercise check was never called"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seen["wt"], capture_output=True, text=True).stdout.strip()
    assert seen["base"] == base and head != base  # the checkpointed HEAD already holds the new code


def test_base_is_required():
    import inspect
    assert inspect.signature(_run_exercise_check).parameters["base"].default is inspect.Parameter.empty


def test_verification_summary_labels_the_exercise_check():
    from navigator.observability.notify import format_verification
    out = format_verification([
        {"argv": "pytest -q -p no:cacheprovider tests/test_mod.py", "returncode": 1, "output_tail": "1 failed",
         "suite": "exercise", "exercised": True},
        {"argv": "pytest -q", "returncode": 0, "output_tail": "5 passed"}])
    assert "exercise check (new tests run against the base): failed as expected" in out
    assert "1 failed" not in out.splitlines()[1]
    out2 = format_verification([{"argv": "pytest x", "returncode": 0, "output_tail": "1 passed",
                                 "suite": "exercise", "exercised": False}])
    assert "exercise check (new tests run against the base): passed on the base — the tests don't exercise the change" in out2
