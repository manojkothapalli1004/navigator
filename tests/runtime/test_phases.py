import subprocess
from collections import Counter
from pathlib import Path
import pytest
from navigator.runtime.worktree import create_worktree
from navigator.runtime.phases import run_build_and_verify, _resolve_write_target, _added_skips, _skip_sites
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


class _ScriptedRoles:
    def __init__(self, writes, commands):
        self.writes, self.commands = writes, commands
    def run_role(self, request):
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=self.writes)
        return DriverResult(role=Role.verifier, commands=self.commands)

def _wt(tmp_path, run_id):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id=run_id, worktree_root=tmp_path/"wt")
    return h, CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")

def test_rejected_verifier_command_is_a_verify_failure_not_a_crash(tmp_path):
    # observed live 2026-10-09: a rejected verifier command raised out of the
    # phase and the run died as an "exception" instead of a verification result
    h, runner = _wt(tmp_path, "run-r")
    drv = _ScriptedRoles([("a.py", "x = 1\n")], [["rm", "-rf", "a.py"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-r", runner=runner,
                               proposal={"affected_files": ["a.py"]})
    assert res.verify_returncode != 0
    assert "rejected" in res.verify_output
    assert (Path(h.path)/"a.py").exists()

def test_builder_cannot_write_files_outside_the_approved_proposal(tmp_path):
    # the operator approved a proposal naming specific files; the builder may not widen it
    h, runner = _wt(tmp_path, "run-s")
    drv = _ScriptedRoles([("a.py", "x = 1\n"), ("sneaky.py", "boom\n")], [["git", "status"]])
    with pytest.raises(ValueError, match="sneaky.py"):
        run_build_and_verify(driver=drv, worktree=h, run_id="run-s", runner=runner,
                             proposal={"affected_files": ["a.py"]})
    assert not (Path(h.path)/"sneaky.py").exists()

def test_failed_test_count_trips_breaker(tmp_path):
    from navigator.safety.circuit_breakers import CircuitBreakers
    from navigator.safety.command_runner import CommandResult
    from navigator.safety.errors import BreakerTripped
    h, _ = _wt(tmp_path, "run-t")
    class _Runner:
        allowlist = ["pytest"]
        def run(self, argv, cwd):
            return CommandResult(1, "=== 7 failed, 3 passed in 0.1s ===", "")
    drv = _ScriptedRoles([("a.py", "x = 1\n")], [["pytest"]])
    cb = CircuitBreakers(max_tool_failures=99, max_identical_failures=99,
                         max_run_duration_s=999, max_failed_tests=5)
    with pytest.raises(BreakerTripped, match="max_failed_tests"):
        run_build_and_verify(driver=drv, worktree=h, run_id="run-t", runner=_Runner(),
                             proposal={"affected_files": ["a.py"]}, breakers=cb)


def test_python_change_without_a_test_command_fails_verification(tmp_path):
    # live 2026-10-10: the verifier passed a .py change in ~48s without running tests
    h, runner = _wt(tmp_path, "run-n")
    (Path(h.path)/"tests").mkdir()  # the repo has a test suite
    drv = _ScriptedRoles([("a.py", "x = 1\n"), ("tests/test_a.py", "def test_a(): pass\n")],
                         [["python3", "-m", "py_compile", "a.py"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-n", runner=runner,
                               proposal={"affected_files": ["a.py"]})
    assert res.verify_returncode != 0
    assert "did not run the test suite" in res.verify_output

def test_docs_only_change_may_verify_without_tests(tmp_path):
    h, runner = _wt(tmp_path, "run-d")
    drv = _ScriptedRoles([("README.md", "hi\n")], [["git", "status"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-d", runner=runner,
                               proposal={"affected_files": ["README.md"]})
    assert res.verify_returncode == 0

def test_verification_evidence_is_recorded(tmp_path):
    h, runner = _wt(tmp_path, "run-e")
    drv = _ScriptedRoles([("README.md", "hi\n")], [["git", "status"], ["git", "log", "--oneline"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-e", runner=runner,
                               proposal={"affected_files": ["README.md"]})
    assert [c["argv"] for c in res.verify_log] == ["git status", "git log --oneline"]
    assert all("returncode" in c and "output_tail" in c for c in res.verify_log)


def test_python_change_in_repo_without_tests_may_use_py_compile(tmp_path):
    h, runner = _wt(tmp_path, "run-p")
    drv = _ScriptedRoles([("a.py", "x = 1\n")], [["python3", "-m", "py_compile", "a.py"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-p", runner=runner,
                               proposal={"affected_files": ["a.py"]})
    assert res.verify_returncode == 0


def test_test_runner_with_uv_flags_counts_as_running_the_suite(tmp_path):
    # waker's suite runs as `uv run --extra dev pytest` (pytest is a dev extra)
    from navigator.runtime.phases import _runs_tests
    assert _runs_tests([["uv", "run", "--extra", "dev", "pytest", "-q"]])
    assert _runs_tests([["uv", "run", "pytest"]]) and _runs_tests([["pytest", "-x"]])
    assert not _runs_tests([["python3", "-m", "py_compile", "a.py"], ["git", "status"]])


def test_verifier_gets_paths_relative_to_its_workdir(tmp_path):
    # live 2026-10-11 (waker): verifier ran `py_compile backend/app/push.py` from inside
    # backend/ because it was handed repo-relative paths -> "No such file"
    repo, _ = _seed(tmp_path)
    (repo/"backend"/"app").mkdir(parents=True); (repo/"backend"/"app"/"x.py").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "b"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-w", worktree_root=tmp_path/"wt")
    runner = CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt")
    drv = _RecordingDriver()
    drv.run_role = (lambda req, real=drv.run_role: real(req) if req.role != Role.builder
                    else (drv.requests.__setitem__(req.role, req) or
                          DriverResult(role=Role.builder, file_writes=[("backend/app/x.py", "y = 1\n")])))
    run_build_and_verify(driver=drv, worktree=h, run_id="run-w", runner=runner,
                         proposal={"affected_files": ["backend/app/x.py"]}, workdir="backend")
    v = drv.requests[Role.verifier].context
    assert v["workdir"] == "backend"
    assert v["changed_files"] == ["app/x.py"]


def test_builder_may_add_test_files_beyond_the_approved_list(tmp_path):
    # tests can't change runtime behaviour; requiring them in affected_files meant a
    # trader gate change shipped with no tests. Non-test files stay out of scope.
    from navigator.runtime.phases import _check_scope
    _check_scope(["a.py", "tests/test_a.py", "pkg/tests/test_b.py", "test_root.py"], ["a.py"])
    with pytest.raises(ValueError, match="b.py"):
        _check_scope(["a.py", "b.py"], ["a.py"])
    with pytest.raises(ValueError, match="conftest_evil"):
        _check_scope(["tests_helpers/conftest_evil.py"], ["a.py"])


def test_code_change_without_any_test_change_fails_in_repo_with_tests(tmp_path):
    # live 2026-10-11: told to add tests, a builder still shipped trade-entry logic
    # with none; the rule is now enforced, not just requested
    h, _ = _wt(tmp_path, "run-q")
    (Path(h.path)/"tests").mkdir()
    runner = CommandRunner(allowlist=["uv run pytest", "git status"], worktree_root=tmp_path/"wt")
    no_tests = _ScriptedRoles([("a.py", "x = 1\n")], [["git", "status"], ["uv", "run", "pytest"]])
    res = run_build_and_verify(driver=no_tests, worktree=h, run_id="run-q", runner=runner,
                               proposal={"affected_files": ["a.py"]})
    assert res.verify_returncode != 0 and "no test changes" in res.verify_output


def test_builder_sees_existing_test_file_names(tmp_path):
    repo, _ = _seed(tmp_path)
    (repo/"tests").mkdir(); (repo/"tests"/"test_x.py").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "t"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-tf", worktree_root=tmp_path/"wt")
    drv = _RecordingDriver()
    run_build_and_verify(driver=drv, worktree=h, run_id="run-tf",
                         runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt"),
                         proposal={"affected_files": ["a.py"]})
    assert drv.requests[Role.builder].context["test_files"] == ["tests/test_x.py"]


def test_workdir_relative_affected_paths_are_mapped_to_repo_paths(tmp_path):
    # live 2026-10-11: waker proposals named "app/routes/pings.py" (relative to the
    # backend/ workdir); built as-is the builder would create a stray root-level copy
    from navigator.runtime.phases import _normalize_affected
    wt = tmp_path/"wt"; (wt/"backend"/"app"/"routes").mkdir(parents=True)
    (wt/"backend"/"app"/"routes"/"pings.py").write_text("")
    (wt/"README.md").write_text("")
    got = _normalize_affected(wt, ["app/routes/pings.py", "README.md", "backend/tests/test_new.py",
                                   "tests/test_new2.py"], "backend")
    assert got == ["backend/app/routes/pings.py", "README.md", "backend/tests/test_new.py",
                   "backend/tests/test_new2.py"]


def test_navigator_runs_the_full_suite_itself_when_repo_has_tests(tmp_path):
    # live 2026-10-11: a waker verifier ran only 3 chosen test files, ignored the
    # allowlist suite its change broke, and "passed"; 13 tests failed on merge
    h, _ = _wt(tmp_path, "run-fs")
    (Path(h.path)/"tests").mkdir()
    calls = []
    from navigator.safety.command_runner import CommandResult
    class _Runner:
        allowlist = ["git status", "uv run pytest"]
        def run(self, argv, cwd):
            calls.append(argv)
            full = argv[:3] == ["uv", "run", "pytest"] and not any(
                a.startswith("tests/") or a == "-k" for a in argv[3:])
            return CommandResult(1 if full else 0, "13 failed" if full else "3 passed", "")
    drv = _ScriptedRoles([("a.py", "x = 1\n"), ("tests/test_a.py", "def test_a(): pass\n")],
                         [["uv", "run", "pytest", "tests/test_a.py"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-fs", runner=_Runner(),
                               proposal={"affected_files": ["a.py"]})
    assert ["uv", "run", "pytest", "-q"] in calls
    assert res.verify_returncode != 0
    assert res.verify_log[-1]["argv"] == "uv run pytest -q"


def test_builder_gets_read_only_context_of_other_repo_files(tmp_path):
    # live 2026-10-11: a waker builder wrote tests creating Users without the
    # required phone_number because it never saw app/models.py
    repo, _ = _seed(tmp_path)
    (repo/"models.py").write_text("class User:\n    phone_number: str\n")
    (repo/"big.py").write_text("x = 1\n" * 20000)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "m"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-ctx", worktree_root=tmp_path/"wt")
    drv = _RecordingDriver()
    run_build_and_verify(driver=drv, worktree=h, run_id="run-ctx",
                         runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt"),
                         proposal={"affected_files": ["a.py"]})
    ctx = drv.requests[Role.builder].context
    assert "phone_number" in ctx["related_files"]["models.py"]
    assert "big.py" not in ctx["related_files"]          # over budget: left out, not truncated
    assert "a.py" not in ctx["related_files"]            # writable files stay in context.files


def test_previous_failure_test_files_are_given_to_the_builder_in_full(tmp_path):
    # live 2026-10-11: a retry still couldn't update tests/test_pings.py — too big
    # for the reference budget, and builders write whole files
    repo, _ = _seed(tmp_path)
    (repo/"backend"/"tests").mkdir(parents=True)
    (repo/"backend"/"tests"/"test_pings.py").write_text("def test_ack(): pass\n" + "# pad\n" * 30000)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "t"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-pf", worktree_root=tmp_path/"wt")
    drv = _RecordingDriver()
    run_build_and_verify(driver=drv, worktree=h, run_id="run-pf", workdir="backend",
                         runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path/"wt"),
                         proposal={"affected_files": ["a.py"],
                                   "previous_attempt_failure": "verification failed: "
                                   "FAILED tests/test_pings.py::test_ack - assert 422 == 200"})
    files = drv.requests[Role.builder].context["files"]
    assert files["backend/tests/test_pings.py"].startswith("def test_ack")


# live 2026-10-12: a run added 11 tests that bail out when a guessed API is missing;
# the suite passed while verifying nothing. Every sample source below is assembled
# from fragments at runtime so this file holds no literal snippets of its own.
SK = "sk" + "ip"
SKIF = SK + "if"
IOS = "importor" + SK
STEST = SK + "Test"
SKIF_U = SK + "If"
SKUN = SK + "Unless"
PM = "pytest" + "mark"
_SKIP_CALL = "pytest." + SK + "("

class _RecordingRunner:
    allowlist = ["git status"]
    def __init__(self):
        self.calls = []
    def run(self, argv, cwd):
        from navigator.safety.command_runner import CommandResult
        self.calls.append(argv)
        return CommandResult(0, "", "")

def test_new_test_file_with_a_skip_fails_verification_without_running_commands(tmp_path):
    h, _ = _wt(tmp_path, "run-sk1")
    runner = _RecordingRunner()
    drv = _ScriptedRoles([("test_new.py", "import pytest\n\ndef test_x():\n    " + _SKIP_CALL + "'no api')\n")],
                         [["git", "status"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-sk1", runner=runner,
                               proposal={"affected_files": []})
    assert res.verify_returncode == 1
    assert res.verify_output == "builder added test skips: test_new.py: 1"
    assert runner.calls == []

def test_existing_skip_left_unchanged_passes(tmp_path):
    h, _ = _wt(tmp_path, "run-sk2")
    original = "import pytest\n\ndef test_a():\n    " + _SKIP_CALL + "'old')\n"
    (Path(h.path)/"test_old.py").write_text(original)
    runner = _RecordingRunner()
    drv = _ScriptedRoles([("test_old.py", original + "\ndef test_b():\n    assert 1\n")],
                         [["git", "status"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-sk2", runner=runner,
                               proposal={"affected_files": []})
    assert res.verify_returncode == 0
    assert runner.calls == [["git", "status"]]

def test_added_xfail_is_not_counted(tmp_path):
    h, _ = _wt(tmp_path, "run-sk3")
    runner = _RecordingRunner()
    drv = _ScriptedRoles([("test_x.py", "import pytest\n\n@pytest.mark.xfail\ndef test_x():\n    assert 0\n")],
                         [["git", "status"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-sk3", runner=runner,
                               proposal={"affected_files": []})
    assert res.verify_returncode == 0

def test_skip_text_in_a_non_test_file_is_not_counted(tmp_path):
    h, _ = _wt(tmp_path, "run-sk4")
    runner = _RecordingRunner()
    drv = _ScriptedRoles([("helper.py", "import pytest\n\ndef bail():\n    " + _SKIP_CALL + "'x')\n")],
                         [["git", "status"]])
    res = run_build_and_verify(driver=drv, worktree=h, run_id="run-sk4", runner=runner,
                               proposal={"affected_files": ["helper.py"]})
    assert res.verify_returncode == 0

def test_skip_counting_covers_all_markers(tmp_path):
    content = (f"import pytest\nm = pytest.{IOS}('x')\n\n@pytest.mark.{SK}\ndef test_a():\n    pass\n\n"
               f"@pytest.mark.{SKIF}(True, reason='r')\ndef test_b():\n    pass\n")
    got = _added_skips(tmp_path, [("tests/test_m.py", content), ("lib.py", content)])
    assert got == ["tests/test_m.py: 3"]


# G1c: ast-based site detection

def _grown(tmp_path, rel, new, old=None):
    if old is not None:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(old)
    return _added_skips(tmp_path, [(rel, new)])


def test_module_level_mark_assignment_trips(tmp_path):
    bare = f"import pytest\n{PM} = pytest.mark.{SK}\n\ndef test_a():\n    assert 1\n"
    called = f"import pytest\n{PM} = pytest.mark.{SK}(reason='r')\n"
    listed = f"import pytest\n{PM} = [pytest.mark.{SKIF}(True, reason='r')]\n"
    assert _skip_sites(bare) == Counter({SK: 1})
    assert _skip_sites(called) == Counter({SK: 1})
    assert _skip_sites(listed) == Counter({SKIF: 1})
    assert _grown(tmp_path, "tests/test_m.py", bare) == ["tests/test_m.py: 1"]


def test_unittest_decorators_trip(tmp_path):
    src = ("import unittest\n\nclass T(unittest.TestCase):\n"
           f"    @unittest.{SK}('r')\n    def test_a(self):\n        pass\n"
           f"    @unittest.{SKIF_U}(True, 'r')\n    def test_b(self):\n        pass\n"
           f"    @unittest.{SKUN}(False, 'r')\n    def test_c(self):\n        pass\n")
    assert _skip_sites(src) == Counter({SK: 1, SKIF_U: 1, SKUN: 1})
    assert _grown(tmp_path, "tests/test_u.py", src) == ["tests/test_u.py: 3"]


def test_self_method_call_trips(tmp_path):
    src = ("import unittest\n\nclass T(unittest.TestCase):\n"
           f"    def test_a(self):\n        self.{STEST}('r')\n")
    assert _skip_sites(src) == Counter({STEST: 1})
    assert _grown(tmp_path, "tests/test_s.py", src) == ["tests/test_s.py: 1"]


def test_call_with_space_before_paren_trips(tmp_path):
    src = f"import pytest\n\ndef test_a():\n    pytest.{SK} ('r')\n"
    assert _skip_sites(src) == Counter({SK: 1})
    assert _grown(tmp_path, "tests/test_sp.py", src) == ["tests/test_sp.py: 1"]


def test_imported_name_call_trips(tmp_path):
    src = f"from pytest import {SK}\n\ndef test_a():\n    {SK}('r')\n"
    assert _skip_sites(src) == Counter({SK: 1})
    assert _grown(tmp_path, "tests/test_i.py", src) == ["tests/test_i.py: 1"]


def test_aliased_module_call_trips(tmp_path):
    src = f"import pytest as pt\n\ndef test_a():\n    pt.{SK}('r')\n"
    assert _skip_sites(src) == Counter({SK: 1})
    assert _grown(tmp_path, "tests/test_al.py", src) == ["tests/test_al.py: 1"]


def test_conftest_new_site_trips(tmp_path):
    src = f"import pytest\n\ndef pytest_runtest_setup(item):\n    pytest.{SK}('r')\n"
    assert _grown(tmp_path, "conftest.py", src) == ["conftest.py: 1"]
    assert _grown(tmp_path, "pkg/conftest.py", src) == ["pkg/conftest.py: 1"]


def test_conditional_marker_counts_once(tmp_path):
    src = f"import pytest\n\n@pytest.mark.{SKIF}(True, reason='r')\ndef test_a():\n    pass\n"
    assert _skip_sites(src) == Counter({SKIF: 1})
    assert _grown(tmp_path, "tests/test_once.py", src) == ["tests/test_once.py: 1"]


def test_unchanged_existing_site_passes(tmp_path):
    old = f"import pytest\n\n@pytest.mark.{SK}\ndef test_a():\n    pass\n"
    new = old + "\ndef test_b():\n    assert 1\n"
    assert _grown(tmp_path, "tests/test_o.py", new, old=old) == []


def test_converting_marker_kind_trips(tmp_path):
    old = f"import pytest\n\n@pytest.mark.{SK}\ndef test_a():\n    pass\n"
    new = f"import pytest\n\n@pytest.mark.{SKIF}(True, reason='r')\ndef test_a():\n    pass\n"
    assert _grown(tmp_path, "tests/test_c.py", new, old=old) == ["tests/test_c.py: 1"]


def test_xfail_marker_passes(tmp_path):
    src = "import pytest\n\n@pytest.mark.xfail\ndef test_a():\n    assert 0\n"
    assert _skip_sites(src) == Counter()
    assert _grown(tmp_path, "tests/test_xf.py", src) == []


def test_names_in_strings_and_comments_do_not_trip(tmp_path):
    src = (f"# pytest.mark.{SKIF}(True)\nX = 'pytest.{SK}(r) {SKIF}( {STEST}('\n\n"
           "def test_a():\n    assert X\n")
    assert _skip_sites(src) == Counter()
    assert _grown(tmp_path, "tests/test_str.py", src) == []


def test_unparseable_source_falls_back_to_text(tmp_path):
    src = f"def broken(:\n    pytest.{SK} ('r')\n    x.{STEST}('r')\n"
    assert _skip_sites(src) == Counter({SK: 1, STEST: 1})


def test_site_in_non_test_module_is_not_counted(tmp_path):
    src = f"import pytest\n\ndef bail():\n    pytest.{SK}('r')\n"
    assert _skip_sites(src) == Counter({SK: 1})
    assert _grown(tmp_path, "pkg/helper.py", src) == []
