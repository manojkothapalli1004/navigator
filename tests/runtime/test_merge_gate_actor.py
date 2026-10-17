"""S3a/S3c: the merge gate honours only operator approvals decided after the run
opened for review. Verification runs builder-written code on the ACT host; an
approval that code writes for its own run must never merge it - and when such an
approval blocks a merge, the operator is told so once (run.merge_blocked)."""
import subprocess
from datetime import datetime, timedelta, timezone

from navigator import cli
from navigator.observability import telegram_inbox
from navigator.runtime import executor
from navigator.runtime.executor import execute_proposal, merge_after_approval, _merge_approved
from navigator.contracts.approval import Approval
from navigator.contracts.run import Run
from navigator.contracts.enums import (RunPhase, RunStatus, ApprovalGate, ApprovalDecision,
                                       Severity)
from tests.runtime.test_executor import _seed, _proposal, _deps

BLOCKED_REASON = "approval not from an operator after review"


def _tracked(repo):
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout


def _decide(d, run, actor):
    a = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(a.id, ApprovalDecision.approved, actor=actor)


def _blocked_events(events):
    return [e for e in events if e.kind == "run.merge_blocked"]


class _StubACT:
    """Only what the gate touches: approvals to read, events to record. Lets the
    test pin decided_at exactly."""
    def __init__(self, approvals):
        self._approvals = list(approvals)
        self.events = []

    def list_approvals(self, target, gate, decision=None):
        return [a for a in self._approvals if a.target == target and a.gate == gate
                and (decision is None or a.decision == decision)]

    def emit_event(self, event):
        self.events.append(event)


def _parked_run(opened):
    return Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-x",
               phase=RunPhase.awaiting_review, heartbeat_at=opened)


def _approval(run, actor, decided_at, decision=ApprovalDecision.approved):
    return Approval(target=run.id, gate=ApprovalGate.merge, decision=decision,
                    actor=actor, decided_at=decided_at)


def test_operator_actor_set_matches_cli_and_telegram_constants():
    assert cli.ACTOR == "operator"
    assert telegram_inbox.TELEGRAM_ACTOR == "operator-telegram"
    assert executor._OPERATOR_ACTORS == {cli.ACTOR, telegram_inbox.TELEGRAM_ACTOR}
    # imported, not re-spelled: renaming an actor cannot lock the operator out
    assert executor.ACTOR is cli.ACTOR
    assert executor.TELEGRAM_ACTOR is telegram_inbox.TELEGRAM_ACTOR
    assert executor._NOT_OPERATOR_REASON == BLOCKED_REASON


def test_approval_by_non_operator_actor_is_ignored_and_reported_once(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, "manoj")
    assert not _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    stored = d["act"].runs[run.id]
    assert stored.status == RunStatus.executing
    assert stored.phase == RunPhase.awaiting_review
    assert "navigator_touch.py" not in _tracked(repo)
    assert not [e for e in d["act"].events if e.kind == "run.completed"]
    blocked = _blocked_events(d["act"].events)
    assert len(blocked) == 1
    assert blocked[0].severity == Severity.warning
    assert blocked[0].payload["run_id"] == run.id
    assert blocked[0].payload["proposal_id"] == run.proposal_id
    assert blocked[0].payload["reason"] == BLOCKED_REASON
    assert blocked[0].payload["actors"] == ["manoj"]
    # later polls do not repeat it
    merge_after_approval(run, **d)
    merge_after_approval(run, **d)
    assert len(_blocked_events(d["act"].events)) == 1
    assert d["act"].runs[run.id].phase == RunPhase.awaiting_review
    assert "navigator_touch.py" not in _tracked(repo)


def test_blocked_notice_is_per_run(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    first = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, first, "manoj")
    merge_after_approval(first, **d)
    second = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, second, "navigator")
    merge_after_approval(second, **d)
    merge_after_approval(second, **d)
    blocked = _blocked_events(d["act"].events)
    assert [e.payload["run_id"] for e in blocked] == [first.id, second.id]
    assert all(e.payload["reason"] == BLOCKED_REASON for e in blocked)


def test_pending_approval_emits_nothing(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    merge_after_approval(run, **d)
    assert _blocked_events(d["act"].events) == []
    assert d["act"].runs[run.id].phase == RunPhase.awaiting_review


def test_approval_by_cli_operator_merges(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, cli.ACTOR)
    assert _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)
    assert _blocked_events(d["act"].events) == []


def test_approval_by_telegram_operator_merges(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _decide(d, run, telegram_inbox.TELEGRAM_ACTOR)
    assert _merge_approved(d["act"], run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)
    assert _blocked_events(d["act"].events) == []


def test_approval_decided_before_awaiting_review_is_ignored():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, cli.ACTOR, opened - timedelta(seconds=1))])
    assert not _merge_approved(act, run)
    # merge_after_approval must bail before touching git or ORC; ACT only receives
    # the merge_blocked notice, once
    out = merge_after_approval(run, act=act, orc=None, driver=None, runner=None, settings=None)
    assert out is run
    assert run.phase == RunPhase.awaiting_review and run.status == RunStatus.executing
    assert [e.kind for e in act.events] == ["run.merge_blocked"]
    assert act.events[0].payload["reason"] == BLOCKED_REASON
    merge_after_approval(run, act=act, orc=None, driver=None, runner=None, settings=None)
    assert len(act.events) == 1


def test_pending_or_declined_approvals_emit_nothing():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, None, None, ApprovalDecision.pending),
                    _approval(run, "manoj", opened, ApprovalDecision.declined)])
    out = merge_after_approval(run, act=act, orc=None, driver=None, runner=None, settings=None)
    assert out is run and act.events == []


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
    merge_after_approval(run, act=act, orc=None, driver=None, runner=None, settings=None)
    assert len(act.events) == 1
    assert act.events[0].payload["actors"] == ["manoj", "navigator"]


def test_one_valid_operator_approval_among_forged_ones_suffices():
    opened = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    run = _parked_run(opened)
    act = _StubACT([_approval(run, "manoj", opened + timedelta(minutes=5)),
                    _approval(run, cli.ACTOR, opened - timedelta(minutes=5)),
                    _approval(run, cli.ACTOR, opened + timedelta(minutes=5))])
    assert _merge_approved(act, run)
