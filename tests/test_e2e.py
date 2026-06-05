import subprocess
from pathlib import Path
from navigator.runtime.loop import Navigator
from navigator.runtime.executor import merge_after_approval
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision, RunStatus

def test_full_loop_propose_approve_execute_review_merge_outcome(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    # propose
    nav.periodic_tick(base_commit_sha=sha)
    p = nav.act.list_proposals(status=ProposalStatus.proposed)[0]
    # operator approves the proposal (review gate)
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    # execute -> awaiting_review
    run = nav.approval_poll()[0]
    merge_appr = nav.act.list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    # operator approves merge
    nav.act.decide_approval(merge_appr.id, ApprovalDecision.approved, actor="manoj")
    merge_after_approval(run, act=nav.act, orc=nav.orc, driver=nav.driver, runner=nav.runner, settings=s)
    assert nav.act.runs[run.id].status == RunStatus.completed
    assert any(sec in ("status","decisions") for sec, _, _ in nav.orc.updates)
    # change is on main
    assert "navigator_touch.py" in subprocess.run(["git","ls-files"], cwd=repo, capture_output=True, text=True).stdout
