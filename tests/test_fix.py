"""B4: the Fix action (runtime/fix.py) - one rebuild grant for a declined run.

A declined run is aborted; its proposal stays approved but loop._may_run needs one
approved review-gate grant per extra run. fix.request_fix writes that grant (the same
record cli._retry / executor._grant_rebuild write) with the note 'fix <run-id>' and a
run.fix_requested event; fix.fix_reason refuses a second grant exactly where
_grant_rebuild does (len(approved review grants) >= len(runs)).
"""
import io
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from navigator.clients.act_client import FakeACTClient
from navigator.contracts.approval import Approval
from navigator.contracts.enums import (ApprovalDecision, ApprovalGate, ProposalStatus, RunPhase,
                                       RunStatus)
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.runtime import fix
from tests.test_cli import _cli, _nav

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ACTOR = "operator-telegram"


def _grants(act, proposal_id):
    return act.list_approvals(target=proposal_id, gate=ApprovalGate.review,
                              decision=ApprovalDecision.approved)


def _fix_events(act):
    return [e for e in act.events if e.kind == "run.fix_requested"]


def _declined(act, title, *, note=None, ended=None, status=ProposalStatus.approved,
              started=None, repo="/r"):
    """A proposal with one declined run: aborted, ended `ended` (default NOW), a declined
    merge approval carrying `note`. Returns (proposal, run)."""
    prop = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash=f"h-{title}",
                                        title=title, rationale="r", repo=repo,
                                        base_commit_sha="abc", intended_changes="x",
                                        affected_files=["a.py"], status=status))
    run = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch=f"nav/{prop.id}",
                             status=RunStatus.aborted, phase=RunPhase.finalizing,
                             correlation_id=f"corr-{prop.id}",
                             started_at=started or (ended or NOW) - timedelta(hours=1),
                             ended_at=ended or NOW))
    appr = act.create_approval(target=run.id, gate=ApprovalGate.merge)
    act.decide_approval(appr.id, ApprovalDecision.declined, actor=ACTOR,
                        **({"note": note} if note else {}))
    return prop, run


# ---- the whole path against the real _may_run ----------------------------------

def test_fix_grants_exactly_one_rebuild_after_a_decline(tmp_path):
    """decline -> fix_reason None -> request_fix -> approval_poll runs once more; a second
    fix is 'already queued' and the grant count stays 1 (the _grant_rebuild rule)."""
    nav, p = _nav(tmp_path)
    _cli(nav, "approve", p.id)
    run = nav.approval_poll()[0]
    assert _cli(nav, "reject", run.id, "--reason", "unsafe", "touches auth")[0] == 0
    nav.merge_poll()
    assert nav.act.runs[run.id].status == RunStatus.aborted
    assert nav.approval_poll() == []  # blocked: runs=1, grants=0

    cands = fix.declined_candidates(nav.act)
    assert [(c[0].id, c[1], c[3].id) for c in cands] == [(run.id, fix.short_name(p.title), p.id)]
    assert cands[0][2] == p.title
    runs = nav.act.list_runs_for_proposal(p.id)
    assert fix.fix_reason(nav.act, cands[0][3], runs) is None
    assert fix.decline_note(nav.act, run.id) == "reason=unsafe: touches auth"

    appr = fix.request_fix(nav.act, cands[0][0], ACTOR, update_id=5)
    grants = _grants(nav.act, p.id)
    assert len(grants) == 1 and grants[0].id == appr.id
    assert grants[0].note == f"fix {run.id}" and grants[0].actor == ACTOR
    ev = _fix_events(nav.act)
    assert len(ev) == 1 and ev[0].correlation_id == run.correlation_id
    assert ev[0].payload == {"run_id": run.id, "proposal_id": p.id, "actor": ACTOR,
                             "update_id": 5}
    # the proposal was approved already and stays so: request_fix never re-approves
    assert nav.act.get_proposal(p.id).status == ProposalStatus.approved

    # second fix before the rebuild starts: refused, nothing written
    assert fix.fix_reason(nav.act, nav.act.get_proposal(p.id),
                          nav.act.list_runs_for_proposal(p.id)) == "already queued"
    assert len(_grants(nav.act, p.id)) == 1

    assert len(nav.approval_poll()) == 1  # exactly one rebuild
    runs = nav.act.list_runs_for_proposal(p.id)
    assert len(runs) == 2
    # the rebuild is the newest run of the proposal: the declined one is no candidate now
    assert fix.declined_candidates(nav.act) == []
    assert fix.fix_reason(nav.act, nav.act.get_proposal(p.id), runs) == "a build is running"
    assert nav.approval_poll() == []  # grant consumed


def test_fix_reason_after_merge_and_for_a_non_approved_proposal():
    act = FakeACTClient()
    prop, run = _declined(act, "S3a: squash merges")
    done = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch="nav/done",
                              status=RunStatus.completed, phase=RunPhase.finalizing,
                              started_at=NOW, ended_at=NOW + timedelta(minutes=5)))
    assert fix.fix_reason(act, prop, [run, done]) == "already merged"
    act.set_proposal_status(prop.id, ProposalStatus.declined)
    assert fix.fix_reason(act, act.get_proposal(prop.id), [run]) == "proposal is declined"
    act.set_proposal_status(prop.id, ProposalStatus.superseded)
    assert fix.fix_reason(act, act.get_proposal(prop.id), [run]) == "proposal is superseded"
    assert fix.fix_reason(act, None, [run]) == "proposal not found"


def test_fix_reason_mirrors_grant_rebuild_arithmetic():
    """refuse when len(approved review grants) >= len(runs); the proposal's own audit
    records (review grants targeting the RUN) never count."""
    act = FakeACTClient()
    prop, run = _declined(act, "S3a: squash merges")
    runs = act.list_runs_for_proposal(prop.id)
    assert fix.fix_reason(act, prop, runs) is None
    audit = act.create_approval(target=run.id, gate=ApprovalGate.review)
    act.decide_approval(audit.id, ApprovalDecision.approved, actor="auditor")
    assert fix.fix_reason(act, prop, runs) is None  # target=run.id: not a grant
    pending = act.create_approval(target=prop.id, gate=ApprovalGate.review)
    assert fix.fix_reason(act, prop, runs) is None  # pending: not counted
    act.decide_approval(pending.id, ApprovalDecision.approved, actor="operator")
    assert fix.fix_reason(act, prop, runs) == "already queued"
    # two declined runs, one grant: one more is allowed
    second = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch="nav/2",
                                status=RunStatus.aborted, phase=RunPhase.finalizing,
                                started_at=NOW, ended_at=NOW + timedelta(minutes=5)))
    runs = act.list_runs_for_proposal(prop.id)
    assert len(runs) == 2
    assert fix.fix_reason(act, prop, runs) is None
    assert second.id in {r.id for r in runs}


# ---- declined_candidates -----------------------------------------------------------

def test_declined_candidates_window_newest_run_and_decline_only():
    act = FakeACTClient()
    fresh_p, fresh = _declined(act, "N5b: Telegram inbox", note="reason=unsafe: auth")
    _declined(act, "Old: eight days", ended=NOW - timedelta(days=8))
    edge_p, edge = _declined(act, "Edge: six days", ended=NOW - timedelta(days=6, hours=23))
    # aborted without a declined merge approval (an N16 quota deferral): not a decline
    deferred = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash="h-def",
                                            title="Q: deferred", rationale="r", repo="/r",
                                            base_commit_sha="abc", intended_changes="x",
                                            status=ProposalStatus.approved))
    act.create_run(Run(proposal_id=deferred.id, base_commit_sha="abc", branch="nav/def",
                       status=RunStatus.aborted, phase=RunPhase.finalizing, ended_at=NOW))
    # a declined run whose proposal has a NEWER run (the rebuild): not a candidate
    rebuilt_p, _rebuilt = _declined(act, "R: rebuilding", ended=NOW - timedelta(hours=2),
                                    started=NOW - timedelta(hours=3))
    act.create_run(Run(proposal_id=rebuilt_p.id, base_commit_sha="abc", branch="nav/r2",
                       status=RunStatus.executing, phase=RunPhase.building, started_at=NOW))
    # a failed run is not a decline
    failed_p = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash="h-f",
                                            title="F: failed", rationale="r", repo="/r",
                                            base_commit_sha="abc", intended_changes="x",
                                            status=ProposalStatus.approved))
    act.create_run(Run(proposal_id=failed_p.id, base_commit_sha="abc", branch="nav/f",
                       status=RunStatus.failed, phase=RunPhase.finalizing, ended_at=NOW))
    # a declined proposal stays listed (amendment 6: fix_reason says 'proposal is declined')
    dp, drun = _declined(act, "D: declined proposal", ended=NOW - timedelta(days=1),
                         status=ProposalStatus.declined)

    cands = fix.declined_candidates(act, now=NOW)
    assert [c[0].id for c in cands] == [fresh.id, drun.id, edge.id]  # newest first
    assert [c[1] for c in cands] == ["N5b", "D", "Edge"]
    assert cands[0][2] == "N5b: Telegram inbox" and cands[0][3].id == fresh_p.id
    assert cands[1][3].status == ProposalStatus.declined
    assert cands[2][3].id == edge_p.id


def test_declined_candidates_capped_and_proposal_failures_skip_the_run(monkeypatch):
    act = FakeACTClient()
    runs = []
    for i in range(fix.MAX_CANDIDATES + 2):
        _, run = _declined(act, f"P{i}: title", ended=NOW - timedelta(minutes=i))
        runs.append(run)
    cands = fix.declined_candidates(act, now=NOW)
    assert len(cands) == fix.MAX_CANDIDATES
    assert [c[0].id for c in cands] == [r.id for r in runs[:fix.MAX_CANDIDATES]]
    # a proposal that cannot be fetched drops only its run
    broken_id = runs[0].proposal_id
    real = act.get_proposal

    def flaky(pid):
        if pid == broken_id:
            raise RuntimeError("ACT hiccup")
        return real(pid)

    monkeypatch.setattr(act, "get_proposal", flaky)
    cands = fix.declined_candidates(act, now=NOW)
    assert broken_id not in {c[3].id for c in cands}
    assert len(cands) == fix.MAX_CANDIDATES
    # a vanished proposal is skipped too
    monkeypatch.setattr(act, "get_proposal", lambda pid: None if pid == broken_id else real(pid))
    assert broken_id not in {c[3].id for c in fix.declined_candidates(act, now=NOW)}


def test_short_name_and_decline_note_helpers():
    assert fix.short_name("N5b: Telegram inbox") == "N5b"
    assert fix.short_name("S3a squash merges") == "S3a"
    assert fix.short_name("") == ""
    act = FakeACTClient()
    _, bare = _declined(act, "A: no note")
    assert fix.decline_note(act, bare.id) is None
    _, noted = _declined(act, "B: noted", note="reason=tests_weak")
    assert fix.decline_note(act, noted.id) == "reason=tests_weak"
    # several declined approvals: the one with a reason= note wins
    _, two = _declined(act, "C: two", note="other words")
    appr = act.create_approval(target=two.id, gate=ApprovalGate.merge)
    act.decide_approval(appr.id, ApprovalDecision.declined, actor=ACTOR,
                        note="reason=unsafe: auth")
    assert fix.decline_note(act, two.id) == "reason=unsafe: auth"
    assert fix.decline_note(act, "run-nope") is None


# ---- request_fix -------------------------------------------------------------------

def test_request_fix_event_failure_keeps_the_grant_and_does_not_raise(monkeypatch):
    """Amendment 8: the grant is written before the event; a failing event log must
    never make the caller tell the operator to re-send (a second grant)."""
    act = FakeACTClient()
    prop, run = _declined(act, "S3a: squash merges")

    def boom(event):
        raise RuntimeError("ACT down")

    monkeypatch.setattr(act, "emit_event", boom)
    appr = fix.request_fix(act, run, ACTOR, update_id=3)
    grants = _grants(act, prop.id)
    assert len(grants) == 1 and grants[0].id == appr.id
    assert grants[0].note == f"fix {run.id}"
    assert fix.fix_reason(act, prop, act.list_runs_for_proposal(prop.id)) == "already queued"


def test_request_fix_default_update_id_and_note_prefix():
    act = FakeACTClient()
    prop, run = _declined(act, "S3a: squash merges")
    fix.request_fix(act, run, "operator-ui")
    (ev,) = _fix_events(act)
    assert ev.payload["update_id"] == 0 and ev.payload["actor"] == "operator-ui"
    assert fix.FIX_NOTE == "fix " and _grants(act, prop.id)[0].note == fix.FIX_NOTE + run.id


def test_fix_module_does_not_import_telegram_inbox_or_cli():
    """Amendment 3: telegram_inbox imports fix; fix must not import it back (cycle)."""
    code = ("import sys, navigator.runtime.fix; "
            "bad = [m for m in ('navigator.observability.telegram_inbox', 'navigator.cli', "
            "'navigator.runtime.executor', 'navigator.runtime.loop') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(fix.__file__).rsplit("/navigator/", 1)[0])
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- B4d: effective grants (one rebuild per decline) and filter-before-cap ---------------

def _grant(act, proposal_id, note=None, actor="operator"):
    """An approved review-gate grant for the proposal: the cli retry / _grant_rebuild
    record when `note` is None, the Fix record ('fix <run-id>') otherwise."""
    appr = act.create_approval(target=proposal_id, gate=ApprovalGate.review)
    act.decide_approval(appr.id, ApprovalDecision.approved, actor=actor,
                        **({"note": note} if note is not None else {}))
    return appr


def _appr(note=None):
    return Approval(target="prop-1", gate=ApprovalGate.review,
                    decision=ApprovalDecision.approved, actor="x", note=note)


def test_effective_grants_counts_plain_grants_plus_distinct_fix_notes():
    """Rev2 item 1: a grant without a 'fix ' note counts each; 'fix <id>' notes count
    once per DISTINCT id, so two taps that both wrote 'fix run1' never buy a second
    rebuild. Pure: a list of Approval objects, note None tolerated."""
    assert fix.effective_grants([]) == 0
    assert fix.effective_grants([_appr()]) == 1                                    # one plain
    assert fix.effective_grants([_appr("fix run-1")]) == 1                         # one fix
    assert fix.effective_grants([_appr("fix run-1"), _appr("fix run-1")]) == 1     # same id
    assert fix.effective_grants([_appr("fix run-1"), _appr("fix run-2")]) == 2     # two ids
    assert fix.effective_grants([_appr(), _appr("fix run-1")]) == 2                # plain + fix
    assert fix.effective_grants([_appr(""), _appr("retry"), _appr("fixed")]) == 3  # not 'fix '
    assert fix.effective_grants([_appr(), _appr(), _appr("fix r"), _appr("fix r"),
                                 _appr("fix s")]) == 4


def test_fix_reason_refuses_a_grant_already_noted_for_the_run():
    """A grant noted 'fix <run.id>' refuses a second Fix for the SAME decline even while
    effective grants < runs; a grant noted for another run id does not block; duplicate
    notes count once; a plain (cli retry) grant counts on its own."""
    act = FakeACTClient()
    prop, run = _declined(act, "S3a: squash merges")
    older = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch="nav/old",
                               status=RunStatus.failed, phase=RunPhase.finalizing,
                               started_at=NOW - timedelta(days=1),
                               ended_at=NOW - timedelta(days=1) + timedelta(minutes=5)))
    runs = act.list_runs_for_proposal(prop.id)
    assert len(runs) == 2
    _grant(act, prop.id, note=f"fix {run.id}")
    assert len(_grants(act, prop.id)) == 1  # < runs: the count rule alone allows
    assert fix.fix_reason(act, prop, runs) is None
    assert fix.fix_reason(act, prop, runs, run=run) == "already queued"  # the note rule
    assert fix.fix_reason(act, prop, runs, run=older) is None  # another run id: no block
    # the ACT button and Telegram raced: a second 'fix <run>' note counts once
    _grant(act, prop.id, note=f"fix {run.id}", actor="operator-ui")
    assert len(_grants(act, prop.id)) == 2
    assert fix.fix_reason(act, prop, runs, run=older) is None  # effective 1 < runs 2
    assert fix.fix_reason(act, prop, runs, run=run) == "already queued"
    _grant(act, prop.id)  # a plain grant: effective 2 >= runs 2
    assert fix.fix_reason(act, prop, runs, run=older) == "already queued"
    assert fix.fix_reason(act, prop, runs) == "already queued"


def test_fixable_candidates_filter_before_the_cap_and_uncapped_declines():
    """12 declines, the 10 newest already queued (a Fix grant each): declined_candidates'
    cap hides the 2 older fixable ones; fixable_candidates returns exactly them.
    limit=None lists every decline, limit=n the n newest."""
    act = FakeACTClient()
    runs = []
    for i in range(fix.MAX_CANDIDATES + 2):
        _, run = _declined(act, f"P{i}: title", ended=NOW - timedelta(minutes=i))
        runs.append(run)
    for run in runs[:fix.MAX_CANDIDATES]:
        _grant(act, run.proposal_id, note=f"fix {run.id}")
    capped = fix.declined_candidates(act, now=NOW)
    assert len(capped) == fix.MAX_CANDIDATES
    assert not {runs[10].id, runs[11].id} & {c[0].id for c in capped}
    fixable = fix.fixable_candidates(act, now=NOW)
    assert [c[0].id for c in fixable] == [runs[10].id, runs[11].id]
    assert [c[1] for c in fixable] == ["P10", "P11"]
    assert [c[2] for c in fixable] == ["P10: title", "P11: title"]
    assert [c[3].id for c in fixable] == [runs[10].proposal_id, runs[11].proposal_id]
    everything = fix.declined_candidates(act, now=NOW, limit=None)
    assert [c[0].id for c in everything] == [r.id for r in runs]
    assert [c[0].id for c in fix.declined_candidates(act, now=NOW, limit=3)] == \
        [r.id for r in runs[:3]]


def test_fixable_candidates_caps_at_max_and_scans_at_most_scan_limit(monkeypatch):
    """At most MAX_CANDIDATES fixable are returned and at most SCAN_LIMIT declines are
    scanned (one fix_reason - one list_runs_for_proposal - per decline scanned)."""
    act = FakeACTClient()
    runs = []
    for i in range(fix.SCAN_LIMIT + 2):
        _, run = _declined(act, f"P{i}: title", ended=NOW - timedelta(minutes=i))
        runs.append(run)
    calls = []
    real = act.list_runs_for_proposal
    monkeypatch.setattr(act, "list_runs_for_proposal",
                        lambda pid: calls.append(pid) or real(pid))
    assert [c[0].id for c in fix.fixable_candidates(act, now=NOW)] == \
        [r.id for r in runs[:fix.MAX_CANDIDATES]]
    assert len(calls) == fix.MAX_CANDIDATES  # stops at the cap
    for run in runs[:fix.SCAN_LIMIT]:
        _grant(act, run.proposal_id, note=f"fix {run.id}")
    calls.clear()
    assert fix.fixable_candidates(act, now=NOW) == []  # the 2 beyond the scan are not reached
    assert len(calls) == fix.SCAN_LIMIT
