from navigator.contracts.enums import (
    ProposalStatus, RunStatus, RunPhase, ApprovalGate, ApprovalDecision, RiskTier, Role, Severity
)

def test_status_vocab_matches_spec():
    assert {s.value for s in ProposalStatus} == {"proposed","approved","declined","expired","superseded"}
    assert {s.value for s in RunStatus} == {"executing","completed","failed","rolled_back","aborted"}
    assert {p.value for p in RunPhase} == {"pending","planning","building","verifying","awaiting_review","merging","finalizing"}
    assert {g.value for g in ApprovalGate} == {"review","merge","architecture","destructive","budget","deploy"}
    assert {d.value for d in ApprovalDecision} == {"pending","approved","declined"}
    assert {r.value for r in Role} == {"planner","builder","verifier"}

def test_v0_implemented_gates():
    assert ApprovalGate.implemented_v0() == {ApprovalGate.review, ApprovalGate.merge}
