import json
import subprocess
from pathlib import Path
import pytest
from navigator.cli import ACTOR
from navigator.runtime.outcome_store import OutcomeStore
from navigator.safety.errors import QuotaExhaustedError
from navigator.runtime.executor import execute_proposal, merge_after_approval
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import RiskTier, RunPhase, ApprovalGate, ApprovalDecision, RunStatus, Role
from navigator.runtime.drivers.base import DriverResult

def _seed(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha

def _proposal(repo, sha):
    return Proposal(title="t", rationale="r", repo=str(repo), base_commit_sha=sha,
                    intended_changes="x", affected_files=["navigator_touch.py"],
                    risk_tier=RiskTier.low, origin="periodic", status="approved",
                    proposal_origin_hash=compute_origin_hash(str(repo),"x",["navigator_touch.py"],"periodic",None))

def _deps(tmp_path, repo):
    s = Settings(worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))  # hermetic: never touch the global /tmp path
    return dict(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                settings=s)

def test_execute_reaches_awaiting_review_and_opens_merge_gate(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.phase == RunPhase.awaiting_review
    assert run.status == RunStatus.executing
    open_merge = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)
    assert len(open_merge) == 1
    # N17: a normal (non-empty) change is unaffected by the empty-diff gate
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=run.worktree_path,
                          capture_output=True, text=True).stdout.strip()
    assert run.verified_sha == head
    parked = [e for e in d["act"].events if e.kind == "run.awaiting_review"][0]
    assert "navigator_touch.py" in parked.payload["diff"]


def test_empty_build_fails_the_run_and_never_parks_at_the_merge_gate(tmp_path):
    # N17, live 2026-10-20: the builder returned no file changes, the verifier passed
    # on the unchanged tree and the run parked at the merge gate with an empty diff
    from navigator.contracts.enums import Role
    from navigator.runtime.drivers.base import DriverResult
    from navigator.runtime.executor import _previous_failure
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)

    class _NoWriteDriver:
        def run_role(self, request):
            if request.role == Role.builder:
                return DriverResult(role=Role.builder, file_writes=[], summary="nothing to do")
            return DriverResult(role=Role.verifier,
                                commands=[["python3", "-m", "py_compile", "navigator_touch.py"]])

    d["driver"] = _NoWriteDriver()
    p = _proposal(repo, sha)
    run = execute_proposal(p, **d)
    assert run.status == RunStatus.failed and run.phase == RunPhase.finalizing
    assert run.verified_sha is None
    assert d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge) == []
    kinds = [e.kind for e in d["act"].events]
    assert "run.failed" in kinds and "run.awaiting_review" not in kinds
    note = [n for sec, n, _ in d["orc"].updates if sec == "known_failures"][-1]
    assert "builder produced no change" in note
    stored = OutcomeStore(d["settings"].outcome_path).for_proposal(p.id)
    assert stored[-1].summary == "builder produced no change"
    # the lesson reaches the next builder
    prev = _previous_failure(d["settings"], p.id)["previous_attempt_failure"]
    assert prev.startswith("builder produced no change")
    assert Path(run.worktree_path).is_dir()  # preserved for inspection

def test_merge_only_after_approval(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    approval = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor=ACTOR)
    merge_after_approval(run, **d)
    merged = subprocess.run(["git","log","--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "navigator-checkpoint" in merged or "Merge" in merged
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert d["orc"].updates  # outcome written

def test_successful_run_appends_outcome_to_store(tmp_path):
    from navigator.runtime.outcome_store import OutcomeStore
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["settings"].outcome_path = str(tmp_path / "outcomes.jsonl")
    run = execute_proposal(_proposal(repo, sha), **d)
    approval = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor=ACTOR)
    merge_after_approval(run, **d)
    stored = OutcomeStore(tmp_path / "outcomes.jsonl").for_proposal(run.proposal_id)
    assert [o.run_id for o in stored] == [run.id]
    assert stored[0].status == "completed"

def test_store_write_failure_does_not_fail_run(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    # point the store at a path whose parent is a FILE → mkdir/open raises
    clash = tmp_path / "afile"; clash.write_text("x")
    d["settings"].outcome_path = str(clash / "sub" / "outcomes.jsonl")
    run = execute_proposal(_proposal(repo, sha), **d)  # must NOT raise
    assert run.phase == RunPhase.awaiting_review  # run proceeded normally

def test_create_worktree_failure_marks_run_failed(tmp_path, monkeypatch):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    def boom(**kw):
        raise RuntimeError("git worktree add failed")
    monkeypatch.setattr("navigator.runtime.executor.create_worktree", boom)
    run = execute_proposal(_proposal(repo, sha), **d)  # must NOT raise
    assert run.status == RunStatus.failed
    assert run.phase == RunPhase.finalizing
    assert any(sec == "known_failures" for sec, _, _ in d["orc"].updates)

def test_success_removes_worktree(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    wt_path = run.worktree_path
    assert Path(wt_path).exists()
    approval = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor=ACTOR)
    merge_after_approval(run, **d)
    assert not Path(wt_path).exists()  # worktree cleaned up on success

def test_failed_verify_writes_failure_outcome_and_preserves(tmp_path, monkeypatch):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    # force verify failure by making the verifier command fail
    from navigator.runtime import phases
    real = phases.run_build_and_verify
    def failing(*a, **k):
        r = real(*a, **k); r.verify_returncode = 1; return r
    monkeypatch.setattr("navigator.runtime.executor.run_build_and_verify", failing)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.status == RunStatus.failed
    o_updates = d["orc"].updates
    assert any(sec == "known_failures" for sec, _, _ in o_updates)

def test_execute_and_merge_work_through_protocol_only_client(tmp_path):
    # executor must not reach into Fake-only attributes (.proposals dict);
    # the whole execute -> approve -> merge path must run on the protocol surface
    from tests.support import ProtocolOnlyACT
    repo, sha = _seed(tmp_path)
    inner = FakeACTClient()
    s = Settings(worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"outcomes.jsonl"))
    d = dict(act=ProtocolOnlyACT(inner), orc=FakeORCClient(), driver=FakeSubagentDriver(),
             runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
             settings=s)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.phase == RunPhase.awaiting_review
    appr = inner.list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    inner.decide_approval(appr.id, ApprovalDecision.approved, actor=ACTOR)
    merge_after_approval(run, **d)
    assert inner.runs[run.id].status == RunStatus.completed


def test_execute_branches_from_current_head_not_stale_proposal_base(tmp_path):
    # proposals sit for weeks; executing on their old base guarantees merge
    # conflicts with everything committed since
    repo, stale = _seed(tmp_path); d = _deps(tmp_path, repo)
    subprocess.run(["git","commit","--allow-empty","-m","later"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    run = execute_proposal(_proposal(repo, stale), **d)
    assert run.base_commit_sha == head
    wt_parent = subprocess.run(["git","rev-parse","HEAD~1"], cwd=run.worktree_path,
                               capture_output=True, text=True).stdout.strip()
    assert head in (wt_parent, subprocess.run(["git","merge-base","HEAD",head], cwd=run.worktree_path,
                                               capture_output=True, text=True).stdout.strip())


def test_heartbeat_renewed_on_every_phase_transition(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    seen = []
    real_update = d["act"].update_run
    def recording(r):
        seen.append((r.phase, r.heartbeat_at)); real_update(r)
    d["act"].update_run = recording
    run = execute_proposal(_proposal(repo, sha), **d)
    beats = [hb for _, hb in seen]
    assert len(seen) >= 2
    assert beats == sorted(beats) and beats[-1] > run.started_at


def test_execute_refuses_proposal_without_review_approval(tmp_path):
    import pytest
    from navigator.contracts.enums import ProposalStatus
    from navigator.safety.errors import GateNotApproved
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    p = _proposal(repo, sha); p.status = ProposalStatus.proposed
    with pytest.raises(GateNotApproved):
        execute_proposal(p, **d)
    assert not (tmp_path/"wt").exists() or not any((tmp_path/"wt").iterdir())

def test_run_duration_breaker_fails_run_and_preserves(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["settings"].max_run_duration_s = 0
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.status == RunStatus.failed
    assert any("max_run_duration" in note for _, note, _ in d["orc"].updates)
    assert Path(run.worktree_path).exists()

def test_budget_is_per_run_not_shared_with_planner_spend(tmp_path):
    from navigator.safety.budget_guard import BudgetGuard
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    guard = BudgetGuard(1.0); guard.record_spend(1.0)  # planner exhausted this cycle's spend
    d["driver"].budget_guard = guard
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.phase == RunPhase.awaiting_review


def test_run_records_verification_evidence_and_cost(tmp_path):
    from navigator.safety.budget_guard import BudgetGuard
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    guard = BudgetGuard(5.0)
    d["driver"].budget_guard = guard
    real = d["driver"].run_role
    def costed(req):
        guard.record_spend(0.25); return real(req)
    d["driver"].run_role = costed
    run = execute_proposal(_proposal(repo, sha), **d)
    stored = d["act"].runs[run.id]
    assert stored.verification and stored.verification[0]["argv"].startswith("python3 -m py_compile")
    assert stored.cost_usd == 0.5  # builder + verifier
    assert stored.model_calls == 2  # what the operator sees; the $ figure is only a budget cap
    ev = [e for e in d["act"].events if e.kind == "run.awaiting_review"][0]
    assert ev.payload["verification"] == stored.verification


def test_failure_notes_keep_the_end_of_test_output(tmp_path, monkeypatch):
    # pytest prints its header first and the failures last; keeping the head (as
    # before) recorded "test session starts" and dropped the actual failures
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    from navigator.runtime import phases
    real = phases.run_build_and_verify
    def failing(*a, **k):
        r = real(*a, **k); r.verify_returncode = 1
        r.verify_output = "=== test session starts ===\n" + "x" * 2000 + "\nFAILED tests/t.py::test_x - boom"
        return r
    monkeypatch.setattr("navigator.runtime.executor.run_build_and_verify", failing)
    execute_proposal(_proposal(repo, sha), **d)
    note = [n for sec, n, _ in d["orc"].updates if sec == "known_failures"][-1]
    assert "FAILED tests/t.py::test_x - boom" in note


def test_retry_shows_the_builder_why_the_previous_attempt_failed(tmp_path, monkeypatch):
    # live 2026-10-11: security-fix runs failed on existing tests they hadn't updated;
    # a blind retry would repeat the mistake because the builder never saw the failure
    from navigator.runtime.outcome_store import OutcomeStore
    from navigator.contracts.outcome import Outcome
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    p = _proposal(repo, sha)
    OutcomeStore(d["settings"].outcome_path).append(Outcome(
        run_id="run-old", proposal_id=p.id, status="failed", summary="verification failed",
        lessons=["x"], verification_notes="FAILED tests/test_pings.py::test_ack - assert 422 == 200"))
    seen = {}
    from navigator.runtime import phases
    real = phases.run_build_and_verify
    def spy(*a, **k):
        seen["proposal"] = k["proposal"]; return real(*a, **k)
    monkeypatch.setattr("navigator.runtime.executor.run_build_and_verify", spy)
    execute_proposal(p, **d)
    assert "assert 422 == 200" in seen["proposal"]["previous_attempt_failure"]


def test_retry_prefers_the_last_failure_with_test_output_over_infra_errors(tmp_path, monkeypatch):
    # live 2026-10-11: the latest failure was a session-limit error, so the retry got
    # no test output and repeated the real mistake
    from navigator.runtime.outcome_store import OutcomeStore
    from navigator.contracts.outcome import Outcome
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    p = _proposal(repo, sha); store = OutcomeStore(d["settings"].outcome_path)
    store.append(Outcome(run_id="r1", proposal_id=p.id, status="failed", summary="verification failed",
                         lessons=["x"], verification_notes="FAILED tests/test_flow.py::test_dispatch - 503"))
    store.append(Outcome(run_id="r2", proposal_id=p.id, status="failed",
                         summary="exception: You've hit your session limit", lessons=["limit"]))
    from navigator.runtime.executor import _previous_failure
    assert "FAILED tests/test_flow.py::test_dispatch" in _previous_failure(d["settings"], p.id)["previous_attempt_failure"]


def test_failure_outcome_records_navigator_code_sha(tmp_path, monkeypatch):
    import pytest
    from navigator.runtime import executor
    from navigator.runtime.outcome_store import OutcomeStore
    pkg = Path(executor.__file__).resolve().parent
    expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=pkg, capture_output=True, text=True)
    if expected.returncode != 0 or not expected.stdout.strip():
        pytest.skip("navigator package is not in a git checkout")
    monkeypatch.setattr(executor, "_code_sha_cache", executor._CODE_SHA_UNSET)
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    def boom(**kw):
        raise RuntimeError("git worktree add failed")
    monkeypatch.setattr("navigator.runtime.executor.create_worktree", boom)
    run = execute_proposal(_proposal(repo, sha), **d)
    stored = OutcomeStore(d["settings"].outcome_path).for_proposal(run.proposal_id)
    assert stored and stored[-1].status == "failed"
    assert stored[-1].code_sha and stored[-1].code_sha == expected.stdout.strip()


def test_code_sha_is_computed_once_and_cached(monkeypatch):
    from navigator.runtime import executor
    monkeypatch.setattr(executor, "_code_sha_cache", executor._CODE_SHA_UNSET)
    calls = []
    def fake_run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, stdout="cafe1234\n", stderr="")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    assert executor.code_sha() == "cafe1234"
    assert executor.code_sha() == "cafe1234"
    assert len(calls) == 1


def test_code_sha_is_none_when_git_unavailable(tmp_path, monkeypatch):
    from navigator.runtime import executor
    from navigator.runtime.outcome_store import OutcomeStore
    from navigator.contracts.run import Run
    monkeypatch.setattr(executor, "_code_sha_cache", executor._CODE_SHA_UNSET)
    def no_git(*a, **k):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr(executor.subprocess, "run", no_git)
    run = Run(proposal_id="p", base_commit_sha="s", branch="nav/x")
    s = Settings(worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"o.jsonl"))
    out = executor._write_outcome(FakeORCClient(), run, status="failed", summary="x",
                                  lessons=["y"], settings=s)  # must not raise
    assert out.code_sha is None
    assert OutcomeStore(tmp_path/"o.jsonl").latest(1)[0].code_sha is None


def test_code_sha_is_none_when_git_fails(monkeypatch):
    from navigator.runtime import executor
    monkeypatch.setattr(executor, "_code_sha_cache", executor._CODE_SHA_UNSET)
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 128, stdout="", stderr="fatal: not a git repository"))
    assert executor.code_sha() is None


# --- N16: an account-wide limit defers the run instead of failing it -----------------

RESETS = "2026-10-20T11:20:00+00:00"


class _QuotaDriver:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def run_role(self, request):
        self.calls += 1
        raise QuotaExhaustedError(self.text, marker="session limit", resets_at=RESETS)


def test_quota_exhausted_defers_the_run_and_grants_one_rebuild(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["driver"] = _QuotaDriver("You've hit your session limit")
    p = _proposal(repo, sha)
    with pytest.raises(QuotaExhaustedError):
        execute_proposal(p, **d)
    runs = d["act"].list_runs_for_proposal(p.id)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == RunStatus.aborted and run.phase == RunPhase.finalizing
    assert run.ended_at is not None
    assert run.worktree_path is None and not (tmp_path / "wt" / run.id).exists()
    kinds = [e.kind for e in d["act"].events]
    assert "run.deferred" in kinds and "run.failed" not in kinds
    deferred = [e for e in d["act"].events if e.kind == "run.deferred"][0]
    assert deferred.payload == {"run_id": run.id, "proposal_id": p.id,
                               "error": "QuotaExhaustedError", "resets_at": RESETS}
    scheduled = [e for e in d["act"].events if e.kind == "run.rebuild_scheduled"][0]
    assert scheduled.payload["granted"] is True
    grants = d["act"].list_approvals(target=p.id, gate=ApprovalGate.review,
                                     decision=ApprovalDecision.approved)
    assert len(grants) == 1 and grants[0].actor == ACTOR
    assert any(sec == "status" and "deferred: quota exhausted until " + RESETS in note
               for sec, note, _ in d["orc"].updates)
    assert not any(sec == "known_failures" for sec, _, _ in d["orc"].updates)
    assert OutcomeStore(d["settings"].outcome_path).for_proposal(p.id)[0].status == "deferred"
    assert d["driver"].calls == 1


def test_quota_deferral_never_records_the_sdk_text(tmp_path, capsys):
    # the SDK text may carry a token-bearing login URL: no event, outcome, ORC note, log
    # line or Telegram message may contain it - class name and resets_at only
    from navigator.observability.notify import NotifyingACT
    secret = "tok_9f2c1"
    text = f"You've hit your session limit \u00b7 resets 4:20am https://claude.ai/login?token={secret}"
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    inner = FakeACTClient()
    sent = []

    class _Rec:
        def send(self, t):
            sent.append(t)

    d["act"] = NotifyingACT(inner, _Rec())
    d["driver"] = _QuotaDriver(text)
    p = _proposal(repo, sha)
    with pytest.raises(QuotaExhaustedError):
        execute_proposal(p, **d)
    captured = capsys.readouterr()
    corpus = [json.dumps([e.model_dump(mode="json") for e in inner.events]),
              json.dumps(d["orc"].updates),
              Path(d["settings"].outcome_path).read_text(),
              captured.out, captured.err, "\n".join(sent)]
    for blob in corpus:
        assert secret not in blob and "session limit" not in blob and "claude.ai" not in blob
    assert len(sent) == 1 and "deferred" in sent[0] and RESETS in sent[0]
    assert "QuotaExhaustedError" in sent[0]
    assert "QuotaExhaustedError" in corpus[0]


# --- merge tiers 3a: the tier is recorded on every run.awaiting_review event -----------

def test_awaiting_review_event_records_the_merge_tier(tmp_path):
    # navigator_touch.py is a code file in a repo named "repo" (not trader/navigator): tier B
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    ev = [e for e in d["act"].events
          if e.kind == "run.awaiting_review" and e.payload["run_id"] == run.id][0]
    assert ev.payload["merge_tier"] == "B"


def test_awaiting_review_event_merge_tier_follows_the_repo_floor(tmp_path):
    from navigator.config import RepoConfig
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["settings"].repos = [RepoConfig(path=str(repo), tier_floor="C")]
    run = execute_proposal(_proposal(repo, sha), **d)
    ev = [e for e in d["act"].events
          if e.kind == "run.awaiting_review" and e.payload["run_id"] == run.id][0]
    assert ev.payload["merge_tier"] == "C"


def test_tier_failure_never_loses_the_awaiting_review_event(tmp_path, monkeypatch):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)

    def boom(*a, **k):
        raise RuntimeError("tier broke")

    monkeypatch.setattr("navigator.runtime.executor.classify_for_repo", boom)
    run = execute_proposal(_proposal(repo, sha), **d)  # must NOT raise
    assert run.phase == RunPhase.awaiting_review
    ev = [e for e in d["act"].events
          if e.kind == "run.awaiting_review" and e.payload["run_id"] == run.id][0]
    assert ev.payload["merge_tier"] is None


# --- merge tiers 3a2: NUL-separated names and the tier git call inside the guard -------

class _NamedWriteDriver:
    """Scripted builder writing the given (relpath, content) pairs; the verifier runs
    `git status` (DEFAULT_ALLOWLIST): a .md-only build has no python change to py_compile."""
    def __init__(self, writes):
        self.writes = writes

    def run_role(self, request):
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=list(self.writes), summary="wrote")
        return DriverResult(role=Role.verifier, commands=[["git", "status"]], summary="verify")


def test_awaiting_review_tier_sees_a_double_quote_name_unquoted(tmp_path):
    # NEGATIVE: on main the tier git call printed the name C-quoted, no pattern matched it and
    # the tier was "B"; with -z the name is exact and a docs-only change is "A"
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    files = ['docs/a"b.md']
    d["driver"] = _NamedWriteDriver([(files[0], "# a\n")])
    prop = Proposal(title="t", rationale="r", repo=str(repo), base_commit_sha=sha, intended_changes="x",
                    affected_files=files, risk_tier=RiskTier.low, origin="periodic", status="approved",
                    proposal_origin_hash=compute_origin_hash(str(repo), "x", files, "periodic", None))
    run = execute_proposal(prop, **d)
    assert run.phase == RunPhase.awaiting_review
    ev = [e for e in d["act"].events
          if e.kind == "run.awaiting_review" and e.payload["run_id"] == run.id][0]
    assert ev.payload["merge_tier"] == "A"


def test_tier_git_failure_never_loses_the_awaiting_review_event(tmp_path, monkeypatch):
    # NEGATIVE: on main the tier's _git_run call sat outside the guard and the OSError escaped
    # execute_proposal
    import navigator.runtime.executor as ex
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    real = ex._git_run

    def flaky(cwd, *args):
        if "--no-renames" in args and "--name-only" in args:
            raise OSError("git vanished")
        return real(cwd, *args)

    monkeypatch.setattr(ex, "_git_run", flaky)
    run = execute_proposal(_proposal(repo, sha), **d)  # must NOT raise
    assert run.phase == RunPhase.awaiting_review
    ev = [e for e in d["act"].events
          if e.kind == "run.awaiting_review" and e.payload["run_id"] == run.id][0]
    assert ev.payload["merge_tier"] is None


# ---- reviewer memory B1: the failure writer ----------------------------------------------

LIVE_REPO_MEMORY_ROOT = "/Users/Manoj/calwbot/logs/navigator/repo_memory"


def _memory_lines(root, repo):
    path = Path(root) / f"{Path(repo).name}.jsonl"
    assert path.is_file(), f"no repo memory entry written at {path}"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _failing_build(monkeypatch, extra_file=None):
    """Force a verify failure whose output ends in a pytest FAILED line; optionally leave an
    extra STAGED (uncommitted) file in the worktree that affected_files does not name."""
    from navigator.runtime import phases
    real = phases.run_build_and_verify

    def failing(*a, **k):
        r = real(*a, **k)
        if extra_file:
            wt = Path(k["worktree"].path)
            (wt / extra_file).write_text("x = 1\n")
            subprocess.run(["git", "add", extra_file], cwd=wt, check=True, capture_output=True)
        r.verify_returncode = 1
        r.verify_output = "=== test session starts ===\n" + "x" * 2000 + "\nFAILED tests/t.py::test_x - boom"
        return r
    monkeypatch.setattr("navigator.runtime.executor.run_build_and_verify", failing)


def test_failed_verify_remembers_failure_with_files(tmp_path, monkeypatch):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["settings"].repo_memory = True
    d["settings"].repo_memory_root = str(tmp_path / "mem")
    _failing_build(monkeypatch, extra_file="extra_touched.py")
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.status == RunStatus.failed
    entries = _memory_lines(tmp_path / "mem", repo)
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "failure" and e["source"] == "executor"
    assert e["run_id"] == run.id and e["proposal_id"] == run.proposal_id
    assert e["repo"] == str(repo) and e["title"] == "t"
    assert e["base_sha"] == sha and e["verified_sha"] is None
    assert e["reason_code"] is None
    assert "verification failed" in e["text"] and "FAILED tests/t.py::test_x - boom" in e["text"]
    # affected_files first, then what the build really touched: the staged, uncommitted extra file is
    # NOT in affected_files and NOT in any commit, so only a working-tree-vs-base diff can see it
    assert e["files"][0] == "navigator_touch.py"
    assert "extra_touched.py" in e["files"]
    # the rest of the failure path is unchanged
    assert any(sec == "known_failures" for sec, _, _ in d["orc"].updates)
    stored = OutcomeStore(d["settings"].outcome_path).for_proposal(run.proposal_id)
    assert [o.status for o in stored] == ["failed"]


def test_failure_memory_is_written_even_when_orc_is_down(tmp_path, monkeypatch):
    # rev2 item 3: HTTPORCClient raises on a timeout; the memory entry must not be lost behind it
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    d["settings"].repo_memory = True
    d["settings"].repo_memory_root = str(tmp_path / "mem")
    _failing_build(monkeypatch)

    def boom(section, note, replace=False):
        raise RuntimeError("orc down")
    d["orc"].apply_memory_update = boom
    import pytest
    with pytest.raises(RuntimeError):           # the ORC failure itself still propagates as on main
        execute_proposal(_proposal(repo, sha), **d)
    entries = _memory_lines(tmp_path / "mem", repo)
    assert [e["kind"] for e in entries] == ["failure"]


def test_memory_write_failure_does_not_fail_run(tmp_path, monkeypatch):
    from structlog.testing import capture_logs
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    clash = tmp_path / "afile"; clash.write_text("x")
    d["settings"].repo_memory = True
    d["settings"].repo_memory_root = str(clash / "sub")      # root under a FILE: makedirs raises
    _failing_build(monkeypatch)
    with capture_logs() as logs:
        run = execute_proposal(_proposal(repo, sha), **d)    # must NOT raise
    assert run.status == RunStatus.failed and run.phase == RunPhase.finalizing
    stored = OutcomeStore(d["settings"].outcome_path).for_proposal(run.proposal_id)
    assert [o.status for o in stored] == ["failed"]           # the outcome trail is intact
    assert any(e.kind == "run.failed" for e in d["act"].events)
    warned = [l for l in logs if l["event"] == "repo_memory.write_failed"]
    assert len(warned) == 1 and warned[0]["kind"] == "failure"
    assert not (clash / "sub").exists()


def test_memory_default_root_is_isolated_under_tmp(tmp_path, monkeypatch):
    # rev2 item 2 guard: Settings(repo_memory=True) with NO root resolves through the env, then the module
    # DEFAULT_ROOT, both of which tests/conftest.py points under tmp - the live log dir is never touched
    from navigator.runtime import repo_memory
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY_ROOT", raising=False)   # force the DEFAULT_ROOT fallback
    repo, sha = _seed(tmp_path)
    s = Settings(worktree_root=str(tmp_path / "wt"), outcome_path=str(tmp_path / "outcomes.jsonl"),
                 repo_memory=True)
    assert s.repo_memory_root is None
    d = dict(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
             runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path / "wt"), settings=s)
    _failing_build(monkeypatch)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.status == RunStatus.failed
    assert repo_memory.DEFAULT_ROOT.startswith(str(tmp_path)), repo_memory.DEFAULT_ROOT
    entries = _memory_lines(repo_memory.DEFAULT_ROOT, repo)
    assert [e["kind"] for e in entries] == ["failure"]
    # nothing for this test repo ever lands under the live root (file-level: the directory itself will
    # legitimately exist once the switch is on in production)
    assert not (Path(LIVE_REPO_MEMORY_ROOT) / f"{repo.name}.jsonl").exists()


def test_failure_memory_off_by_default_writes_nothing(tmp_path, monkeypatch):
    # guard: a plain Settings() keeps the failure path byte-identical to before B1
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    assert d["settings"].repo_memory is False
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "1")    # the daemon's Settings value wins over the env
    _failing_build(monkeypatch)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.status == RunStatus.failed
    from navigator.runtime import repo_memory
    assert not Path(repo_memory.root_for(d["settings"])).exists()
    assert not Path(repo_memory.DEFAULT_ROOT).exists()


# ---- reviewer memory B3: what the memory recorded reaches the next build ----------------------

def _memory_on(d, tmp_path):
    d["settings"].repo_memory = True
    d["settings"].repo_memory_root = str(tmp_path / "mem")
    return d["settings"]


def _spy_build(monkeypatch):
    """Record the proposal dict and the repo_memory kwarg handed to run_build_and_verify."""
    from navigator.runtime import phases
    real = phases.run_build_and_verify
    seen = {}

    def spy(*a, **k):
        seen["proposal"] = k["proposal"]
        seen["repo_memory"] = k.get("repo_memory")
        return real(*a, **k)
    monkeypatch.setattr("navigator.runtime.executor.run_build_and_verify", spy)
    return seen


def test_decline_text_rule():
    # rev2 item 2: "reason=CODE<sep>text" -> text; bare code or nothing -> "no reason given"
    from navigator.runtime import executor
    fn = getattr(executor, "_decline_text", None)
    assert fn, "executor._decline_text is missing"
    assert fn("reason=unsafe: x", "unsafe") == "x"
    assert fn("reason=low_value; meh", "low_value") == "meh"
    assert fn("reason=unsafe", "unsafe") == "no reason given"
    assert fn("reason=unsafe:   ", "unsafe") == "no reason given"
    assert fn("reason=unsafe touches auth", "unsafe") == "touches auth"
    assert fn("looks wrong", None) == "looks wrong"
    assert fn("  looks wrong \n", "unsafe") == "looks wrong"
    assert fn("", None) == "no reason given"
    assert fn(None, "unsafe") == "no reason given"
    assert executor.DECLINE_CHARS == 500


def test_rebuild_sees_the_decline_reason_and_repo_memory(tmp_path, monkeypatch):
    from navigator.runtime import repo_memory
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    s = _memory_on(d, tmp_path)
    p = _proposal(repo, sha)
    assert repo_memory.remember(s, str(repo), kind="decline", run_id="run-old", proposal_id=p.id,
                                reason_code="unsafe", text="reason=unsafe: touches auth",
                                files=["navigator_touch.py"], affected_files=["navigator_touch.py"],
                                source="operator", title="t") is True
    assert repo_memory.remember(s, str(repo), kind="failure", run_id="run-x", proposal_id="prop-other",
                                text="verification failed: FAILED tests/t.py::test_y", files=["navigator_touch.py"],
                                source="executor", title="another proposal on the same file") is True
    # an entry for an unrelated file never reaches this build
    assert repo_memory.remember(s, str(repo), kind="audit", run_id="run-z", proposal_id="prop-z",
                                text="elsewhere", files=["other.py"], source="auditor") is True
    seen = _spy_build(monkeypatch)
    run = execute_proposal(p, **d)
    assert run.phase == RunPhase.awaiting_review
    assert seen["proposal"].get("previous_decline") == "declined (unsafe): touches auth"
    assert "previous_attempt_failure" not in seen["proposal"]      # no failed outcome: untouched
    mem = seen.get("repo_memory")
    assert mem, "repo_memory kwarg missing or empty"
    assert mem[0]["kind"] == "failure"
    assert mem[0]["title"] == "another proposal on the same file"
    assert mem[0]["files"] == ["navigator_touch.py"]
    assert set(mem[0]) == {"kind", "reason_code", "text", "files", "ts", "title"}
    # the rendered decline is not repeated in the list; the other file's audit is filtered out
    assert [e["kind"] for e in mem] == ["failure"]


def test_same_proposal_failures_stay_out_of_repo_memory(tmp_path, monkeypatch):
    # previous_attempt_failure already carries the last failure in full; a same-proposal audit stays
    from navigator.runtime import repo_memory
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    s = _memory_on(d, tmp_path)
    p = _proposal(repo, sha)
    f = ["navigator_touch.py"]
    repo_memory.remember(s, str(repo), kind="failure", run_id="r1", proposal_id=p.id, text="MINE-FAIL", files=f)
    repo_memory.remember(s, str(repo), kind="audit", run_id="r2", proposal_id=p.id, text="MINE-AUDIT", files=f)
    repo_memory.remember(s, str(repo), kind="failure", run_id="r3", proposal_id="prop-other", text="OTHER-FAIL",
                         files=f)
    repo_memory.remember(s, str(repo), kind="failure", run_id="r4", proposal_id=p.id, text="MINE-FAIL-2", files=f)
    # same basename, another repo: never shown (B1's accepted collision)
    other_repo = tmp_path / "elsewhere" / repo.name
    repo_memory.remember(s, str(other_repo), kind="decline", run_id="r5", proposal_id="prop-q",
                         text="reason=unsafe: foreign", files=f, reason_code="unsafe")
    seen = _spy_build(monkeypatch)
    run = execute_proposal(p, **d)
    assert run.phase == RunPhase.awaiting_review
    assert "previous_decline" not in seen["proposal"]
    mem = seen.get("repo_memory")
    assert mem, "repo_memory kwarg missing or empty"
    assert [e["text"] for e in mem] == ["OTHER-FAIL", "MINE-AUDIT"]      # newest first


def test_memory_off_injects_nothing(tmp_path, monkeypatch):
    # GUARD (green on main): entries on disk, switch off -> proposal dict byte-identical to main, no
    # repo_memory list, and NO file read at all (recent() raising proves it is never called)
    from navigator.runtime import repo_memory
    from types import SimpleNamespace
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    root = str(tmp_path / "mem")
    writer = SimpleNamespace(repo_memory=True, repo_memory_root=root)
    p = _proposal(repo, sha)
    assert repo_memory.remember(writer, str(repo), kind="decline", run_id="run-old", proposal_id=p.id,
                                reason_code="unsafe", text="reason=unsafe: touches auth",
                                files=["navigator_touch.py"]) is True
    d["settings"].repo_memory_root = root
    assert d["settings"].repo_memory is False

    def boom(*a, **k):
        raise AssertionError("repo_memory.recent was called while the switch is off")
    monkeypatch.setattr(repo_memory, "recent", boom)
    seen = _spy_build(monkeypatch)
    run = execute_proposal(p, **d)
    assert run.phase == RunPhase.awaiting_review
    assert seen["proposal"] == {"title": "t", "rationale": "r", "intended_changes": "x",
                                "affected_files": ["navigator_touch.py"]}
    assert seen["repo_memory"] is None


def test_memory_read_failure_warns_and_builds(tmp_path, monkeypatch):
    # rev2 item 1: recent() swallows OSError itself, so a reader fault is simulated by raising
    from structlog.testing import capture_logs
    from navigator.runtime import repo_memory
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    _memory_on(d, tmp_path)
    p = _proposal(repo, sha)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(repo_memory, "recent", boom)
    seen = _spy_build(monkeypatch)
    with capture_logs() as logs:
        run = execute_proposal(p, **d)          # must NOT raise
    assert run.phase == RunPhase.awaiting_review and run.status == RunStatus.executing
    warned = [l for l in logs if l["event"] == "repo_memory.read_failed"]
    assert len(warned) == 1, [l["event"] for l in logs]
    assert warned[0]["log_level"] == "warning"
    assert warned[0]["proposal_id"] == p.id and warned[0]["error"] == "RuntimeError"
    assert not any("disk on fire" in str(v) for v in warned[0].values())   # class name only
    assert "previous_decline" not in seen["proposal"]
    assert seen["repo_memory"] is None


def test_grant_rebuild_counts_duplicate_fix_grants_once(tmp_path):
    """B4d: two Fix grants both noted 'fix <run1>' are ONE effective grant, so a stale
    failure of run2 still earns its rebuild grant (raw count 2 >= 2 would have refused);
    once that plain grant exists (effective 2 >= runs 2) _grant_rebuild refuses."""
    from navigator.contracts.run import Run
    from navigator.runtime.executor import _grant_rebuild
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    act = d["act"]
    p = act.create_proposal(_proposal(repo, sha))
    run1 = act.create_run(Run(proposal_id=p.id, base_commit_sha=sha, branch="nav/run-1",
                              status=RunStatus.aborted, phase=RunPhase.finalizing))
    run2 = act.create_run(Run(proposal_id=p.id, base_commit_sha=sha, branch="nav/run-2",
                              status=RunStatus.failed, phase=RunPhase.finalizing))
    for actor in ("operator-telegram", "operator-ui"):
        g = act.create_approval(target=p.id, gate=ApprovalGate.review)
        act.decide_approval(g.id, ApprovalDecision.approved, actor=actor, note=f"fix {run1.id}")

    def grants():
        return act.list_approvals(target=p.id, gate=ApprovalGate.review,
                                  decision=ApprovalDecision.approved)

    assert len(grants()) == 2
    assert _grant_rebuild(act, run2) is True
    plain = [g for g in grants() if g.note is None]
    assert len(grants()) == 3 and len(plain) == 1 and plain[0].actor == ACTOR
    assert _grant_rebuild(act, run2) is False  # effective 2 (one fix id + one plain) >= 2
    assert len(grants()) == 3
