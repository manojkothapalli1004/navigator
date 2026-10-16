"""S3a: the merge gate honours only operator approvals decided after the run opened
for review. Verification runs builder-written code on the ACT host; an approval that
code writes for its own run must never merge it."""
import subprocess
from datetime import datetime, timedelta, timezone

from navigator import cli
from navigator.observability import telegram_inbox
from navigator.runtime import executor
from navigator.runtime.executor import execute_proposal, merge_after_approval, _merge_approved
from navigator.contracts.approval import Approval
from navigator.contracts.run import Run
from navigator.contracts.enums import RunPhase, RunStatus, ApprovalGate, ApprovalDecision
from tests.runtime.test_executor import _seed, _proposal, _deps


def _tracked(repo):
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout


def _decide(d, run, actor):
    a = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(a.id, ApprovalDecision.approved, actor=actor)


class _StubACT:
    """Only what _merge_approved touches; lets the test pin decided_at exactly."""
    def __init__(self, approvals):
        self._approvals = list(approvals)

    def list_approvals(self, target, gate, decision=None):
        return [a for a in self._approvals if a.target == target and a.gate == gate
                and (decision is None or a.decision == decision)]


def _parked_run(opened):
    return Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-x",
               phase=RunPhase.awaiting_review, heartbeat_at=opened)


def _approval(run, actor, decided_at):
    return Approval(target=run.id, gate=ApprovalGate.merge, decision=ApprovalDecision.approved,
                    actor=actor, decided_at=decided_at)


def test_operator_actor_set_matches_cli_and_telegram_constants():
    assert cli.ACTOR == "operator"
    assert telegram_inbox.TELEGRAM_ACTOR == "operator-telegram"
    assert executor._OPERATOR_ACTORS == {cli.ACTOR, telegram_inbox.TELEGRAM_ACTOR}


def test_approval_by_non_operator_actor_is_ignored(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, "manoj")
    assert not _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    stored = d["act"].runs[run.id]
    assert stored.status == RunStatus.executing
    assert stored.phase == RunPhase.awaiting_review
    assert "navigator_touch.py" not in _tracked(repo)
    assert not [e for e in d["act"].events if e.kind in ("run.completed", "run.merge_blocked")]


def test_approval_by_cli_operator_merges(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, cli.ACTOR)
    assert _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)


def test_approval_by_telegram_operator_merges(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, telegram_inbox.TELEGRAM_ACTOR)
    assert _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)


def test_approval_decided_before_awaiting_review_is_ignored():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, cli.ACTOR, opened - timedelta(seconds=1))])
    assert not _merge_approved(act, run)
    # merge_after_approval must bail before touching git, ACT or ORC
    out = merge_after_approval(run, act=act, orc=None, driver=None, runner=None, settings=None)
    assert out is run
    assert run.phase == RunPhase.awaiting_review and run.status == RunStatus.executing


def test_approval_decided_at_same_instant_is_accepted():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    assert _merge_approved(_StubACT([_approval(run, cli.ACTOR, opened)]), run)
    assert _merge_approved(_StubACT([_approval(run, cli.ACTOR, opened + timedelta(seconds=1))]), run)


def test_naive_decided_at_is_treated_as_utc():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    assert _merge_approved(_StubACT([_approval(run, cli.ACTOR, datetime(2026, 10, 16, 12, 0, 0))]), run)
    assert not _merge_approved(
        _StubACT([_approval(run, cli.ACTOR, datetime(2026, 10, 16, 11, 59, 59))]), run)


def test_approval_without_decided_at_is_judged_on_actor_alone():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    assert _merge_approved(_StubACT([_approval(run, cli.ACTOR, None)]), run)
    assert _merge_approved(_StubACT([_approval(run, telegram_inbox.TELEGRAM_ACTOR, None)]), run)
    assert not _merge_approved(_StubACT([_approval(run, "manoj", None)]), run)


def test_late_non_operator_approval_never_counts():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, "manoj", opened + timedelta(minutes=5)),
                    _approval(run, "navigator", opened + timedelta(minutes=5))])
    assert not _merge_approved(act, run)


def test_one_valid_operator_approval_among_forged_ones_suffices():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, "manoj", opened + timedelta(minutes=5)),
                    _approval(run, cli.ACTOR, opened - timedelta(minutes=5)),
                    _approval(run, cli.ACTOR, opened + timedelta(minutes=5))])
    assert _merge_approved(act, run)
