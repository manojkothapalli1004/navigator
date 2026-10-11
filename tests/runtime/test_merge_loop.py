import subprocess
from pathlib import Path
from navigator.runtime.loop import Navigator
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.enums import (ProposalStatus, ApprovalGate, ApprovalDecision,
                                       RunStatus, RunPhase)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _setup(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    nav.periodic_tick(base_commit_sha=sha)
    p = nav.act.list_proposals(status=ProposalStatus.proposed)[0]
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    run = nav.approval_poll()[0]
    assert run.phase == RunPhase.awaiting_review
    return nav, repo, run


def _decide(nav, run, decision):
    appr = nav.act.list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    nav.act.decide_approval(appr.id, decision, actor="manoj")


def test_merge_poll_merges_after_operator_approval(tmp_path):
    # merge_after_approval existed but nothing in the loop ever called it, so a
    # verified run sat at the merge gate forever
    nav, repo, run = _setup(tmp_path)
    assert nav.merge_poll() == []  # not approved yet: nothing happens
    _decide(nav, run, ApprovalDecision.approved)
    done = nav.merge_poll()
    assert [r.id for r in done] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _git(repo, "ls-files")


def test_declined_merge_aborts_run_and_preserves_worktree(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _decide(nav, run, ApprovalDecision.declined)
    nav.merge_poll()
    stored = nav.act.runs[run.id]
    assert stored.status == RunStatus.aborted
    assert stored.phase == RunPhase.finalizing
    assert Path(run.worktree_path).exists()
    assert "navigator_touch.py" not in _git(repo, "ls-files")
    assert any("declined" in note for _, note, _ in nav.orc.updates)


def test_merge_blocked_while_main_checkout_dirty(tmp_path):
    nav, repo, run = _setup(tmp_path)
    (repo/"tracked.txt").write_text("a")
    _git(repo, "add", "tracked.txt"); _git(repo, "commit", "-m", "t")
    (repo/"tracked.txt").write_text("operator's uncommitted edit")
    _decide(nav, run, ApprovalDecision.approved)
    assert nav.merge_poll() == []
    stored = nav.act.runs[run.id]
    assert stored.phase == RunPhase.awaiting_review  # parked, retried next tick
    assert (repo/"tracked.txt").read_text() == "operator's uncommitted edit"
    assert any(e.kind == "run.merge_blocked" for e in nav.act.events)


def test_merge_blocked_when_checkout_on_other_branch(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    _decide(nav, run, ApprovalDecision.approved)
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review


def test_merge_conflict_aborts_cleanly_and_fails_run(tmp_path):
    nav, repo, run = _setup(tmp_path)
    (repo/"navigator_touch.py").write_text("conflicting = True\n")
    _git(repo, "add", "navigator_touch.py"); _git(repo, "commit", "-m", "conflict")
    _decide(nav, run, ApprovalDecision.approved)
    nav.merge_poll()
    stored = nav.act.runs[run.id]
    assert stored.status == RunStatus.failed
    assert _git(repo, "status", "--porcelain").strip() == ""  # merge aborted, tree clean
    assert (repo/"navigator_touch.py").read_text() == "conflicting = True\n"
    assert Path(run.worktree_path).exists()


def _setup_squash(tmp_path):
    nav, repo, run = _setup(tmp_path)
    nav.squash = True
    _git(repo, "config", "user.name", "Owner"); _git(repo, "config", "user.email", "owner@users.noreply.github.com")
    return nav, repo, run


def test_public_repo_merge_is_one_commit_under_the_repo_identity(tmp_path):
    # trader/orchestrator are public: navigator@local checkpoint commits and tool
    # identities must never become part of their history
    nav, repo, run = _setup_squash(tmp_path)
    before = _git(repo, "rev-parse", "HEAD").strip()
    _decide(nav, run, ApprovalDecision.approved)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    new = _git(repo, "rev-list", f"{before}..HEAD").split()
    assert len(new) == 1
    assert _git(repo, "log", "-1", "--format=%an <%ae>").strip() == "Owner <owner@users.noreply.github.com>"
    assert "navigator" not in _git(repo, "log", "--format=%an %ae %B", "-1").lower()
    assert "navigator-checkpoint" not in _git(repo, "log", "--format=%s")
    assert run.branch not in _git(repo, "branch", "--list")
    assert "navigator_touch.py" in _git(repo, "ls-files")


def test_public_repo_squash_conflict_leaves_checkout_clean(tmp_path):
    nav, repo, run = _setup_squash(tmp_path)
    (repo/"navigator_touch.py").write_text("conflicting = True\n")
    _git(repo, "add", "navigator_touch.py"); _git(repo, "commit", "-m", "conflict")
    _decide(nav, run, ApprovalDecision.approved)
    nav.merge_poll()
    assert nav.act.runs[run.id].status == RunStatus.failed
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert (repo/"navigator_touch.py").read_text() == "conflicting = True\n"
