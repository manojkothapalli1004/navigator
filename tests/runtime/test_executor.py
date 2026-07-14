import subprocess
from pathlib import Path
from navigator.runtime.executor import execute_proposal, merge_after_approval
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import RiskTier, RunPhase, ApprovalGate, ApprovalDecision, RunStatus

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
                    risk_tier=RiskTier.low, origin="periodic",
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

def test_merge_only_after_approval(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    approval = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor="manoj")
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
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor="m")
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

def test_create_worktree_failure_marks_run_failed(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    bad = _proposal(repo, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")  # invalid base sha
    run = execute_proposal(bad, **d)  # must NOT raise
    assert run.status == RunStatus.failed
    assert run.phase == RunPhase.finalizing
    assert any(sec == "known_failures" for sec, _, _ in d["orc"].updates)

def test_success_removes_worktree(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    wt_path = run.worktree_path
    assert Path(wt_path).exists()
    approval = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(approval.id, ApprovalDecision.approved, actor="manoj")
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
    inner.decide_approval(appr.id, ApprovalDecision.approved, actor="manoj")
    merge_after_approval(run, **d)
    assert inner.runs[run.id].status == RunStatus.completed
