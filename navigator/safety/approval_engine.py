from navigator.contracts.enums import ApprovalGate, ApprovalDecision, ProposalStatus
from navigator.safety.errors import GateNotApproved, GateNotImplemented


def require_gate(act, gate: ApprovalGate, target: str) -> None:
    """Raise unless the operator has approved `gate` for `target` in ACT."""
    if gate == ApprovalGate.review:
        p = act.get_proposal(target)
        if p is None or p.status != ProposalStatus.approved:
            raise GateNotApproved(f"review gate: proposal {target} is not approved")
        return
    if gate == ApprovalGate.merge:
        if not act.list_approvals(target=target, gate=gate, decision=ApprovalDecision.approved):
            raise GateNotApproved(f"merge gate: run {target} has no approved merge approval")
        return
    raise GateNotImplemented(f"gate {gate.value!r} is schema-only")
