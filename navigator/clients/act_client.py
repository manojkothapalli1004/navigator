from datetime import datetime, timezone
from typing import Protocol
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.contracts.approval import Approval
from navigator.contracts.event import Event
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision

class ACTClient(Protocol):
    def create_proposal(self, p: Proposal) -> Proposal: ...
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]: ...
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None: ...
    def create_run(self, r: Run) -> Run: ...
    def update_run(self, r: Run) -> None: ...
    def list_open_runs(self) -> list[Run]: ...
    def create_approval(self, target: str, gate: ApprovalGate) -> Approval: ...
    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str) -> None: ...
    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]: ...
    def emit_event(self, event: Event) -> None: ...

class FakeACTClient:
    def __init__(self):
        self.proposals: dict[str, Proposal] = {}
        self.runs: dict[str, Run] = {}
        self.approvals: dict[str, Approval] = {}
        self.events: list[Event] = []
    def create_proposal(self, p: Proposal) -> Proposal:
        self.proposals[p.id] = p; return p
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        return [p for p in self.proposals.values() if status is None or p.status == status]
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.proposals[proposal_id].status = status
    def create_run(self, r: Run) -> Run:
        self.runs[r.id] = r; return r
    def update_run(self, r: Run) -> None:
        self.runs[r.id] = r
    def list_open_runs(self) -> list[Run]:
        from navigator.contracts.enums import RunStatus
        return [r for r in self.runs.values() if r.status == RunStatus.executing]
    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        a = Approval(target=target, gate=gate); self.approvals[a.id] = a; return a
    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str) -> None:
        a = self.approvals[approval_id]
        a.decision = decision; a.actor = actor; a.decided_at = datetime.now(timezone.utc)
    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        return [a for a in self.approvals.values()
                if a.target == target and a.gate == gate
                and (decision is None or a.decision == decision)]
    def emit_event(self, event: Event) -> None:
        self.events.append(event)

_INERT = "HTTPACTClient is inert until the ACT agent-resource patch is applied (see act-patch/)."

class HTTPACTClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
    def _refuse(self, *a, **k):
        raise NotImplementedError(_INERT)
    create_proposal = list_proposals = set_proposal_status = _refuse
    create_run = update_run = list_open_runs = _refuse
    create_approval = decide_approval = list_approvals = emit_event = _refuse
