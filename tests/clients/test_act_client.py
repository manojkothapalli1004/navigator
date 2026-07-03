import pytest
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import ProposalStatus, RiskTier, ApprovalGate, ApprovalDecision

def _proposal():
    return Proposal(title="t", rationale="r", repo="trader", base_commit_sha="abc",
                    intended_changes="x", affected_files=["a.py"], risk_tier=RiskTier.low,
                    origin="periodic",
                    proposal_origin_hash=compute_origin_hash("trader","x",["a.py"],"periodic",None))

def test_fake_act_list_returns_copies_not_live_refs():
    # mutating a returned object must NOT corrupt the in-memory store
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    got = act.list_proposals(status=ProposalStatus.proposed)[0]
    got.status = ProposalStatus.approved  # mutate the returned object
    # store is unchanged: still proposed, not approved
    assert act.list_proposals(status=ProposalStatus.proposed)[0].id == p.id
    assert act.list_proposals(status=ProposalStatus.approved) == []

def test_fake_act_proposal_and_approval_flow():
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    assert act.list_proposals(status=ProposalStatus.proposed)[0].id == p.id
    act.set_proposal_status(p.id, ProposalStatus.approved)
    assert act.list_proposals(status=ProposalStatus.approved)[0].id == p.id
    a = act.create_approval(target=p.id, gate=ApprovalGate.merge)
    act.decide_approval(a.id, ApprovalDecision.approved, actor="manoj")
    assert act.list_approvals(target=p.id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.approved)

def test_http_act_is_inert_until_patch():
    act = HTTPACTClient(base_url="http://localhost:9")
    with pytest.raises(NotImplementedError):
        act.create_proposal(_proposal())

# ---- protocol additions for HTTP wiring ------------------------------------

def test_fake_act_get_proposal_returns_copy_or_none():
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    got = act.get_proposal(p.id)
    assert got is not None and got.id == p.id
    got.status = ProposalStatus.approved  # mutating the copy must not touch the store
    assert act.get_proposal(p.id).status == ProposalStatus.proposed
    assert act.get_proposal("proposal-missing") is None

def test_fake_act_list_runs_for_proposal_includes_completed():
    from navigator.contracts.run import Run
    from navigator.contracts.enums import RunStatus
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    r1 = Run(proposal_id=p.id, base_commit_sha="abc", branch="nav/r1")
    r2 = Run(proposal_id="proposal-other", base_commit_sha="abc", branch="nav/r2")
    act.create_run(r1); act.create_run(r2)
    r1.status = RunStatus.completed; act.update_run(r1)
    got = act.list_runs_for_proposal(p.id)
    assert [r.id for r in got] == [r1.id]           # completed run still visible
    assert got[0].status == RunStatus.completed
    got[0].status = RunStatus.executing              # copy, not live ref
    assert act.list_runs_for_proposal(p.id)[0].status == RunStatus.completed
