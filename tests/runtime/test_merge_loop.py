import io
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from navigator.cli import ACTOR, is_audited, run_cli
from navigator.contracts.proposal import Proposal
from navigator.runtime.loop import Navigator
from navigator.runtime.executor import (AUTO_ACTOR, _AUTO_RECHECK_REASON, _NOT_OPERATOR_REASON,
                                        suite_green, base_on_head)
from navigator.runtime.drivers.base import DriverResult
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.enums import (ProposalStatus, ApprovalGate, ApprovalDecision,
                                       RunStatus, RunPhase, Role)


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


def _decide(nav, run, decision, actor=ACTOR):
    appr = nav.act.list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    nav.act.decide_approval(appr.id, decision, actor=actor)


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


def test_merge_commit_message_says_what_changed(tmp_path):
    # live 2026-10-11: waker re-proposed an already-merged cooldown because its merge
    # commit only said "navigator merge run-…" and the planner reads commit subjects
    nav, repo, run = _setup(tmp_path)
    title = nav.act.get_proposal(run.proposal_id).title
    _decide(nav, run, ApprovalDecision.approved)
    nav.merge_poll()
    subject = _git(repo, "log", "-1", "--format=%s").strip()
    assert title in subject and run.id in subject


# ---- N8: merge decisions in dry_run (opt-in) ----------------------------------

def _to_dry_run(nav, flag):
    # the run was built in execute mode; from here the daemon runs in dry_run
    nav.settings.mode = "dry_run"
    nav.settings.merge_in_dry_run = flag


def test_dry_run_without_flag_leaves_approved_run_unmerged(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _decide(nav, run, ApprovalDecision.approved)
    _to_dry_run(nav, False)
    assert nav.settings.merge_in_dry_run is False  # the code default: today's behaviour
    assert nav.merge_poll() == []
    stored = nav.act.runs[run.id]
    assert stored.phase == RunPhase.awaiting_review and stored.status == RunStatus.executing
    assert "navigator_touch.py" not in _git(repo, "ls-files")


def test_dry_run_with_flag_merges_approved_run(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _to_dry_run(nav, True)
    assert nav.merge_poll() == []  # not decided yet: still nothing
    _decide(nav, run, ApprovalDecision.approved)
    done = nav.merge_poll()
    assert [r.id for r in done] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _git(repo, "ls-files")


def test_dry_run_with_flag_declines_declined_run(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _to_dry_run(nav, True)
    _decide(nav, run, ApprovalDecision.declined)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    stored = nav.act.runs[run.id]
    assert stored.status == RunStatus.aborted and stored.phase == RunPhase.finalizing
    assert "navigator_touch.py" not in _git(repo, "ls-files")


def test_dry_run_with_flag_still_builds_nothing(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _to_dry_run(nav, True)
    p = nav.act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash="hash-second", title="second: another change",
        rationale="r", repo=str(repo), base_commit_sha=run.base_commit_sha,
        intended_changes="add b.py", affected_files=["b.py"]))
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    _decide(nav, run, ApprovalDecision.approved)
    assert nav.approval_poll() == []  # the flag only unlocks merge_poll
    assert nav.act.list_runs_for_proposal(p.id) == []
    assert [r.id for r in nav.merge_poll()] == [run.id]
    assert nav.approval_poll() == []
    assert nav.act.list_runs_for_proposal(p.id) == []


def test_execute_mode_is_unaffected_by_the_flag(tmp_path):
    nav, repo, run = _setup(tmp_path)
    assert nav.settings.mode == "execute"
    nav.settings.merge_in_dry_run = True
    _decide(nav, run, ApprovalDecision.approved)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed


# ---- N10: a build in another process holds the lock; merge_poll defers quietly ------

def test_merge_poll_defers_while_lock_held_then_merges_after_release(tmp_path):
    # live 2026-10-17: every daemon cycle during a once.sh build logged error
    # 'single-flight lock held' because merge_poll raised instead of waiting a cycle
    from structlog.testing import capture_logs
    from navigator.runtime.lock import SingleFlightLock
    nav, repo, run = _setup(tmp_path)
    _decide(nav, run, ApprovalDecision.approved)
    with SingleFlightLock(tmp_path/"nav.lock").acquire():  # this live process is the holder
        with capture_logs() as logs:
            assert nav.merge_poll() == []  # deferred, not raised
        assert (tmp_path/"nav.lock").exists()
    assert any(e["event"] == "merge_poll.deferred_lock_held" for e in logs)
    stored = nav.act.runs[run.id]
    assert stored.phase == RunPhase.awaiting_review and stored.status == RunStatus.executing
    assert "navigator_touch.py" not in _git(repo, "ls-files")
    # the decision was recorded in ACT; the next cycle after release acts on it
    assert [r.id for r in nav.merge_poll()] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _git(repo, "ls-files")


def test_merge_poll_still_raises_unrelated_lock_errors(tmp_path):
    import pytest
    nav, repo, run = _setup(tmp_path)
    _decide(nav, run, ApprovalDecision.approved)
    def boom():
        raise RuntimeError("disk on fire")
    nav.lock.acquire = boom
    with pytest.raises(RuntimeError, match="disk on fire"):
        nav.merge_poll()
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review


# ---- 3b: tier A auto-merge (NAVIGATOR_MERGE_TIER_A_AUTO) --------------------------------

class _DocsDriver:
    """Scripted driver: one docs-only file, verified with `git status` (tier A)."""

    def __init__(self, path="docs/notes.md"):
        self.path = path

    def run_role(self, request):
        if request.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=[(self.path, "# notes\n")])
        return DriverResult(role=Role.verifier, commands=[["git", "status"]])


def _docs_setup(tmp_path, path="docs/notes.md", extra_commit=False):
    # exactly _setup, but a scripted docs-only run instead of the fake navigator_touch.py one
    repo = tmp_path/"repo"; repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    if extra_commit:
        _git(repo, "commit", "--allow-empty", "-m", "extra")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=_DocsDriver(path),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    p = nav.act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash="h-docs", title="Docs: notes", rationale="r",
        repo=str(repo), base_commit_sha=sha, intended_changes="x", affected_files=[path]))
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    run = nav.approval_poll()[0]
    assert run.phase == RunPhase.awaiting_review
    assert run.base_commit_sha == sha
    return nav, repo, run


def _audit(nav, run):
    assert run_cli(["audited", run.id], act=nav.act, out=io.StringIO()) == 0
    assert is_audited(nav.act, run.id)


def _merge_approvals(nav, run):
    return nav.act.list_approvals(target=run.id, gate=ApprovalGate.merge)


def _auto_approvals(nav, run):
    return [a for a in _merge_approvals(nav, run) if a.actor == AUTO_ACTOR]


def _events(nav, kind):
    return [e for e in nav.act.events if e.kind == kind]


def _dirty_checkout(repo):
    # the same way test_merge_blocked_while_main_checkout_dirty blocks the preflight
    (repo/"tracked.txt").write_text("a")
    _git(repo, "add", "tracked.txt"); _git(repo, "commit", "-m", "t")
    (repo/"tracked.txt").write_text("operator's uncommitted edit")


def _clean_checkout(repo):
    _git(repo, "checkout", "--", "tracked.txt")
    assert _git(repo, "status", "--porcelain", "--untracked-files=no").strip() == ""


def test_tier_a_docs_only_run_auto_merges_when_flag_on(tmp_path):
    nav, repo, run = _docs_setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    # NEGATIVE: not audited yet - nothing happens, no auto approval
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert _auto_approvals(nav, run) == []
    assert "docs/notes.md" not in _git(repo, "ls-files")
    _audit(nav, run)
    done = nav.merge_poll()
    assert [r.id for r in done] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    assert "docs/notes.md" in _git(repo, "ls-files")
    approvals = _merge_approvals(nav, run)
    approved = [a for a in approvals if a.decision == ApprovalDecision.approved]
    assert approved and all(a.actor == AUTO_ACTOR for a in approved)
    # the original request (closed by the post-merge leftover close) plus the auto one
    assert len(approvals) == 2
    assert len(_events(nav, "run.auto_merge_approved")) == 1
    completed = _events(nav, "run.completed")
    assert len(completed) == 1
    assert completed[0].payload["auto"] is True
    assert completed[0].payload["revert"].startswith(f"git -C {repo} revert -m 1 ")
    assert completed[0].payload["merge_sha"] == _git(repo, "rev-parse", "HEAD").strip()


def test_leftover_operator_approval_is_closed_after_auto_merge(tmp_path):
    nav, repo, run = _docs_setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    _audit(nav, run)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    approvals = _merge_approvals(nav, run)
    assert approvals and all(a.decision != ApprovalDecision.pending for a in approvals)
    out = io.StringIO()
    rc = run_cli(["reject", run.id, "--reason", "other"], act=nav.act, out=out)
    assert rc == 1
    assert f"no pending merge approval for {run.id}" in out.getvalue()
    assert "applied on the daemon's next cycle" not in out.getvalue()
    assert nav.act.runs[run.id].status == RunStatus.completed


def test_rewound_main_blocks_auto_merge(tmp_path):
    # NEGATIVE: the operator reset main past the run's base; a tier A classification of
    # base..verified would otherwise merge the dropped commit back in
    nav, repo, run = _docs_setup(tmp_path, extra_commit=True)
    _audit(nav, run)
    _git(repo, "reset", "--hard", "HEAD~1")
    assert _git(repo, "rev-parse", "HEAD").strip() != run.base_commit_sha
    nav.settings.merge_tier_a_auto = True
    assert base_on_head(str(repo), run) is False
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert _events(nav, "run.auto_merge_approved") == []
    assert _auto_approvals(nav, run) == []
    assert "docs/notes.md" not in _git(repo, "ls-files")


def test_reject_after_auto_approval_still_declines(tmp_path):
    # NEGATIVE: the auto approval is a NEW record; the operator's pending one survives and
    # a decline on it still wins
    nav, repo, run = _docs_setup(tmp_path)
    _audit(nav, run)
    nav.settings.merge_tier_a_auto = True
    _dirty_checkout(repo)
    assert nav.merge_poll() == []  # auto-approved, but the preflight blocked the merge
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    approvals = _merge_approvals(nav, run)
    assert [a.actor for a in approvals if a.decision == ApprovalDecision.approved] == [AUTO_ACTOR]
    assert [a.decision for a in approvals if a.actor != AUTO_ACTOR] == [ApprovalDecision.pending]
    _decide(nav, run, ApprovalDecision.declined)
    _clean_checkout(repo)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    stored = nav.act.runs[run.id]
    assert stored.status == RunStatus.aborted and stored.phase == RunPhase.finalizing
    assert stored.status != RunStatus.completed
    assert _events(nav, "run.completed") == []
    assert "docs/notes.md" not in _git(repo, "ls-files")


def test_orc_failure_after_merge_keeps_the_note(tmp_path, monkeypatch):
    nav, repo, run = _docs_setup(tmp_path)
    _audit(nav, run)
    nav.settings.merge_tier_a_auto = True

    def boom(*args, **kwargs):
        raise RuntimeError("ORC ReadTimeout")
    monkeypatch.setattr(nav.orc, "apply_memory_update", boom)
    done = nav.merge_poll()  # must not raise
    assert [r.id for r in done] == [run.id]
    assert done[0].status == RunStatus.completed
    assert nav.act.runs[run.id].status == RunStatus.completed
    completed = _events(nav, "run.completed")
    assert len(completed) == 1
    assert completed[0].payload["revert"].startswith(f"git -C {repo} revert -m 1 ")
    assert completed[0].payload["merge_sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert "docs/notes.md" in _git(repo, "ls-files")


def test_tier_b_run_never_auto_merges(tmp_path):
    # NEGATIVE: navigator_touch.py is tier B even when audited and green
    nav, repo, run = _setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    _audit(nav, run)
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert _events(nav, "run.auto_merge_approved") == []
    assert _auto_approvals(nav, run) == []
    assert "navigator_touch.py" not in _git(repo, "ls-files")


def test_c_pattern_docs_run_never_auto_merges(tmp_path):
    # NEGATIVE: audited and green, so ONLY the tier check (docs/SECRETS.md matches *secret*) stops it
    nav, repo, run = _docs_setup(tmp_path, path="docs/SECRETS.md")
    _audit(nav, run)
    assert suite_green(run.verification, ["docs/SECRETS.md"])
    nav.settings.merge_tier_a_auto = True
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert _auto_approvals(nav, run) == []
    assert _events(nav, "run.auto_merge_approved") == []
    assert "docs/SECRETS.md" not in _git(repo, "ls-files")


def test_flag_off_never_auto_merges(tmp_path):
    # NEGATIVE: the flag check in _auto_approve_tier_a itself, not just the merge-time one
    nav, repo, run = _docs_setup(tmp_path)
    _audit(nav, run)
    assert nav.settings.merge_tier_a_auto is False
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert _auto_approvals(nav, run) == []
    assert _events(nav, "run.auto_merge_approved") == []
    assert "docs/notes.md" not in _git(repo, "ls-files")


def test_stale_auto_approval_does_not_block_reapproval(tmp_path):
    nav, repo, run = _docs_setup(tmp_path)
    _audit(nav, run)
    nav.settings.merge_tier_a_auto = True
    _dirty_checkout(repo)
    assert nav.merge_poll() == []  # auto-approved without merging (preflight blocked)
    assert len(_auto_approvals(nav, run)) == 1
    # simulate a rebase: the run re-opened for review after that auto approval was decided
    time.sleep(0.01)
    run.heartbeat_at = datetime.now(timezone.utc)
    nav.act.update_run(run)
    assert run_cli(["audited", run.id], act=nav.act, out=io.StringIO()) == 0
    _clean_checkout(repo)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    approvals = _merge_approvals(nav, run)
    # the stale auto one, the fresh auto one, and the operator's original pending one that
    # the post-merge leftover-close decided
    assert len(approvals) == 3
    assert [a.actor for a in approvals].count(AUTO_ACTOR) == 3
    assert not any(a.decision == ApprovalDecision.pending for a in approvals)
    assert "docs/notes.md" in _git(repo, "ls-files")


def test_auto_approve_failure_does_not_escape(tmp_path, monkeypatch):
    nav, repo, run = _docs_setup(tmp_path)
    _audit(nav, run)  # FIRST: the audit itself calls decide_approval
    nav.settings.merge_tier_a_auto = True

    def boom(*args, **kwargs):
        raise RuntimeError("ACT down")
    monkeypatch.setattr(nav.act, "decide_approval", boom)
    assert nav.merge_poll() == []  # must not raise
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    pending = [a for a in _merge_approvals(nav, run) if a.decision == ApprovalDecision.pending]
    assert len(pending) == 2  # the operator's plus exactly one orphan
    assert _events(nav, "run.auto_merge_approved") == []
    # orphan guard: a second poll (decide still raising) does not add another
    assert nav.merge_poll() == []
    assert len(_merge_approvals(nav, run)) == 2
    assert len([a for a in _merge_approvals(nav, run)
                if a.decision == ApprovalDecision.pending]) == 2
    assert "docs/notes.md" not in _git(repo, "ls-files")


def test_forged_auto_approval_on_tier_b_is_ignored(tmp_path):
    # NEGATIVE: code that writes the auto actor's approval for its own tier B run merges nothing
    nav, repo, run = _setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    _decide(nav, run, ApprovalDecision.approved, actor=AUTO_ACTOR)
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert "navigator_touch.py" not in _git(repo, "ls-files")
    blocked = _events(nav, "run.merge_blocked")
    assert [e.payload["reason"] for e in blocked] == [_AUTO_RECHECK_REASON]
    assert blocked[0].payload["actors"] == [AUTO_ACTOR]
    assert _events(nav, "run.auto_merge_approved") == []


def test_forged_stranger_approval_on_tier_b_is_ignored(tmp_path):
    nav, repo, run = _setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    _decide(nav, run, ApprovalDecision.approved, actor="stranger")
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert "navigator_touch.py" not in _git(repo, "ls-files")
    blocked = _events(nav, "run.merge_blocked")
    assert [e.payload["reason"] for e in blocked] == ["approval not from an operator after review"]
    assert _NOT_OPERATOR_REASON == "approval not from an operator after review"


def test_forged_auto_approval_on_unaudited_tier_a_is_ignored(tmp_path):
    # NEGATIVE: auto_merge_ok re-checks is_audited at merge time
    nav, repo, run = _docs_setup(tmp_path)
    nav.settings.merge_tier_a_auto = True
    _decide(nav, run, ApprovalDecision.approved, actor=AUTO_ACTOR)
    assert nav.merge_poll() == []
    assert nav.act.runs[run.id].phase == RunPhase.awaiting_review
    assert "docs/notes.md" not in _git(repo, "ls-files")
    blocked = _events(nav, "run.merge_blocked")
    assert len(blocked) == 1
    assert blocked[0].payload["reason"] == _AUTO_RECHECK_REASON
    assert _events(nav, "run.completed") == []


def test_operator_merge_still_works_and_reports_revert(tmp_path):
    nav, repo, run = _setup(tmp_path)
    _decide(nav, run, ApprovalDecision.approved)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    assert nav.act.runs[run.id].status == RunStatus.completed
    completed = _events(nav, "run.completed")
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["auto"] is False
    prefix = f"git -C {repo} revert -m 1 "
    assert payload["revert"].startswith(prefix)
    assert payload["revert"][len(prefix):] == _git(repo, "rev-parse", "HEAD").strip()
    assert payload["merge_sha"] == _git(repo, "rev-parse", "HEAD").strip()


def test_squash_merge_reports_plain_revert(tmp_path):
    nav, repo, run = _setup_squash(tmp_path)
    _decide(nav, run, ApprovalDecision.approved)
    assert [r.id for r in nav.merge_poll()] == [run.id]
    payload = _events(nav, "run.completed")[0].payload
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert payload["revert"] == f"git -C {repo} revert {head}"
    assert "-m 1" not in payload["revert"]


def test_suite_green_rules():
    assert suite_green([], ["a.py"]) is False
    assert suite_green([], ["docs/x.md"]) is False
    git_only = [{"argv": "git status", "argv_list": ["git", "status"], "returncode": 0}]
    assert suite_green(git_only, ["docs/x.md"]) is True
    assert suite_green(git_only, ["a.py"]) is False  # no pytest for a .py change
    uv = [{"argv_list": ["uv", "run", "pytest", "-q"], "returncode": 0}]
    assert suite_green(uv, ["tests/test_a.py"]) is True
    assert suite_green([{"argv_list": ["uv", "run", "pytest", "-q"], "returncode": 1}],
                       ["tests/test_a.py"]) is False
    assert suite_green([{"argv_list": ["uv", "run", "pytest", "-q"], "returncode": 0},
                        {"argv_list": ["git", "status"], "returncode": 1}],
                       ["tests/test_a.py"]) is False  # last entry failed
    assert suite_green([{"argv": "pytest -q", "returncode": 0}], ["tests/test_a.py"]) is True
    assert suite_green([{"argv": "uv run pytest -q", "returncode": 0}], ["a.py"]) is True
    assert suite_green([{"argv": "python3 -m py_compile a.py", "returncode": 0}], ["a.py"]) is False
