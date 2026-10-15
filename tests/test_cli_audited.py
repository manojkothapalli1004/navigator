import io

from navigator.cli import is_audited, run_cli
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.run import Run


def _cli(act, *argv):
    out = io.StringIO()
    rc = run_cli(list(argv), act=act, out=out)
    return rc, out.getvalue()


def _run(act, phase=RunPhase.awaiting_review, pending=True):
    run = act.create_run(Run(proposal_id="proposal-x", base_commit_sha="abc",
                             branch="nav/x", phase=phase))
    if pending:
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    return run


def test_audited_marks_awaiting_review_run():
    act = FakeACTClient()
    run = _run(act)
    assert not is_audited(act, run.id)
    rc, out = _cli(act, "audited", run.id, "--note", "diff reviewed")
    assert rc == 0, out
    assert is_audited(act, run.id)
    events = [e for e in act.events if e.kind == "run.audited"]
    assert len(events) == 1
    assert events[0].payload == {"run_id": run.id, "note": "diff reviewed", "actor": "auditor"}
    grants = act.list_approvals(target=run.id, gate=ApprovalGate.review,
                                decision=ApprovalDecision.approved)
    assert len(grants) == 1 and grants[0].actor == "auditor"
    # the merge gate itself is untouched: auditing never approves a merge
    assert len(act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.pending)) == 1


def test_audited_without_note():
    act = FakeACTClient()
    run = _run(act)
    rc, _ = _cli(act, "audited", run.id)
    assert rc == 0
    ev = [e for e in act.events if e.kind == "run.audited"][0]
    assert ev.payload["note"] is None


def test_audited_twice_is_idempotent():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited", run.id)[0] == 0
    rc, out = _cli(act, "audited", run.id)
    assert rc == 0 and "already" in out
    assert len(act.list_approvals(target=run.id, gate=ApprovalGate.review)) == 1


def test_audited_refuses_run_not_awaiting_review():
    act = FakeACTClient()
    run = _run(act, phase=RunPhase.building)
    rc, out = _cli(act, "audited", run.id)
    assert rc == 1
    assert "not awaiting_review" in out
    assert not is_audited(act, run.id)
    assert [e for e in act.events if e.kind == "run.audited"] == []


def test_audited_refuses_unknown_run():
    act = FakeACTClient()
    rc, out = _cli(act, "audited", "run-01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert rc == 1 and "no open run" in out


def test_audited_bad_arguments():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited")[0] == 2
    assert _cli(act, "audited", run.id, "--bogus", "x")[0] == 2
    assert _cli(act, "audited", run.id, "--note")[0] == 2
    assert not is_audited(act, run.id)


def test_cli_merge_still_works_on_unaudited_run():
    act = FakeACTClient()
    run = _run(act)
    assert not is_audited(act, run.id)
    rc, out = _cli(act, "merge", run.id)
    assert rc == 0, out
    approved = act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.approved)
    assert len(approved) == 1 and approved[0].actor == "operator"
