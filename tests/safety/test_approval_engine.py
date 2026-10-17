import pytest
from navigator.cli import ACTOR
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.proposal import Proposal
from navigator.contracts.enums import ApprovalGate, ApprovalDecision, ProposalStatus
from navigator.safety.approval_engine import require_gate
from navigator.safety.errors import GateNotApproved, GateNotImplemented


def _proposal(status):
    return Proposal(title="t", rationale="r", repo="/r", base_commit_sha="a", intended_changes="x",
                    affected_files=["a.py"], origin="periodic", proposal_origin_hash="h", status=status)

def test_review_gate_requires_approved_proposal():
    act = FakeACTClient()
    p = act.create_proposal(_proposal(ProposalStatus.proposed))
    with pytest.raises(GateNotApproved):
        require_gate(act, ApprovalGate.review, p.id)
    act.set_proposal_status(p.id, ProposalStatus.approved)
    require_gate(act, ApprovalGate.review, p.id)

def test_merge_gate_requires_approved_merge_approval():
    act = FakeACTClient()
    a = act.create_approval(target="run-1", gate=ApprovalGate.merge)
    with pytest.raises(GateNotApproved):
        require_gate(act, ApprovalGate.merge, "run-1")
    act.decide_approval(a.id, ApprovalDecision.approved, actor=ACTOR)
    require_gate(act, ApprovalGate.merge, "run-1")

@pytest.mark.parametrize("gate", [ApprovalGate.architecture, ApprovalGate.destructive,
                                  ApprovalGate.budget, ApprovalGate.deploy])
def test_schema_only_gates_are_not_implemented(gate):
    with pytest.raises(GateNotImplemented):
        require_gate(FakeACTClient(), gate, "x")
