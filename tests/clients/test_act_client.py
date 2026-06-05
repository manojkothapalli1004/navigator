import pytest
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import ProposalStatus, RiskTier, ApprovalGate, ApprovalDecision

def _proposal():
    return Proposal(title="t", rationale="r", repo="trader", base_commit_sha="abc",
                    intended_changes="x", affected_files=["a.py"], risk_tier=RiskTier.low,
                    origin="periodic",
                    proposal_origin_hash=compute_origin_hash("trader","x",["a.py"],"periodic",None))

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
