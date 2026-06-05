from navigator.contracts.approval import Approval
from navigator.contracts.enums import ApprovalGate, ApprovalDecision

def test_approval_defaults():
    a = Approval(target="proposal-1", gate=ApprovalGate.review)
    assert a.id.startswith("approval-")
    assert a.decision == ApprovalDecision.pending
