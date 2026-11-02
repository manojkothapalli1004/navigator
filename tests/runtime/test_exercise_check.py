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
