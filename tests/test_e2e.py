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
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))
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


def test_operator_journey_through_daemon_cycles_and_cli_with_restart(tmp_path):
    """propose -> CLI approve -> cycle builds -> restart -> CLI merge -> cycle merges"""
    import io
    from navigator.__main__ import _cycle
    from navigator.cli import run_cli
    from navigator.contracts.enums import RunPhase
    repo = tmp_path/"repo"; repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    s = Settings(mode="execute", repo=str(repo), worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"), lock_path=str(tmp_path/"nav.lock"))
    act, orc = FakeACTClient(), FakeORCClient()
    def daemon():  # a fresh process each time: nothing survives but ACT/ORC/git
        nav = Navigator(act=act, orc=orc, driver=FakeSubagentDriver(),
                        runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                        settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
        nav.reconcile_on_startup()
        return nav
    cli = lambda *a: run_cli(list(a), act=act, out=io.StringIO())

    nav = daemon()
    assert _cycle(nav, s)["proposals_created"] == 1
    p = act.list_proposals(status=ProposalStatus.proposed)[0]
    assert cli("approve", p.id) == 0
    assert _cycle(nav, s)["runs_executed"] == 1
    run = next(iter(act.runs.values()))
    assert run.phase == RunPhase.awaiting_review

    nav = daemon()  # restart while parked at the merge gate
    assert act.runs[run.id].status == RunStatus.executing
    assert cli("merge", run.id) == 0
    assert _cycle(nav, s)["runs_finalized"] == 1
    assert act.runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in subprocess.run(["git", "ls-files"], cwd=repo,
                                                  capture_output=True, text=True).stdout
    assert _cycle(nav, s)["runs_executed"] == 0  # completed proposal never re-runs
