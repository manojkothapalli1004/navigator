"""Test doubles shared across suites.

ProtocolOnlyACT delegates to a FakeACTClient but exposes ONLY the ACTClient
protocol surface — runtime code that reaches into Fake-only attributes
(.proposals / .runs dicts) fails against it, exactly as it would against
HTTPACTClient.
"""
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.contracts.approval import Approval
from navigator.contracts.event import Event
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision


class ProtocolOnlyACT:
    def __init__(self, inner: FakeACTClient):
        self._inner = inner

    def create_proposal(self, p: Proposal) -> Proposal:
        return self._inner.create_proposal(p)

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._inner.get_proposal(proposal_id)

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        return self._inner.list_proposals(status)

    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        return self._inner.set_proposal_status(proposal_id, status)

    def create_run(self, r: Run) -> Run:
        return self._inner.create_run(r)

    def update_run(self, r: Run) -> None:
        return self._inner.update_run(r)

    def list_open_runs(self) -> list[Run]:
        return self._inner.list_open_runs()

    def list_all_runs(self) -> list[Run]:
        return self._inner.list_all_runs()

    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]:
        return self._inner.list_runs_for_proposal(proposal_id)

    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        return self._inner.create_approval(target, gate)

    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str) -> None:
        return self._inner.decide_approval(approval_id, decision, actor)

    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        return self._inner.list_approvals(target, gate, decision)

    def emit_event(self, event: Event) -> None:
        return self._inner.emit_event(event)

    def get_doc(self, key: str) -> tuple[dict, int] | None:
        return self._inner.get_doc(key)

    def put_doc(self, key: str, body: dict, actor: str,
                expected_version: int | None = None) -> int:
        return self._inner.put_doc(key, body, actor, expected_version)
