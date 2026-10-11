from datetime import datetime, timezone
from typing import Protocol
import httpx
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.contracts.approval import Approval
from navigator.contracts.event import Event
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision

class ACTClient(Protocol):
    def create_proposal(self, p: Proposal) -> Proposal: ...
    def get_proposal(self, proposal_id: str) -> Proposal | None: ...
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]: ...
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None: ...
    def create_run(self, r: Run) -> Run: ...
    def update_run(self, r: Run) -> None: ...
    def list_open_runs(self) -> list[Run]: ...
    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]: ...
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
    def get_proposal(self, proposal_id: str) -> Proposal | None:
        p = self.proposals.get(proposal_id)
        return p.model_copy(deep=True) if p is not None else None
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        return [p.model_copy(deep=True) for p in self.proposals.values()
                if status is None or p.status == status]
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.proposals[proposal_id].status = status
    def create_run(self, r: Run) -> Run:
        self.runs[r.id] = r; return r
    def update_run(self, r: Run) -> None:
        self.runs[r.id] = r
    def list_open_runs(self) -> list[Run]:
        from navigator.contracts.enums import RunStatus
        return [r.model_copy(deep=True) for r in self.runs.values() if r.status == RunStatus.executing]
    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]:
        return [r.model_copy(deep=True) for r in self.runs.values() if r.proposal_id == proposal_id]
    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        a = Approval(target=target, gate=gate); self.approvals[a.id] = a; return a
    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str) -> None:
        a = self.approvals[approval_id]
        a.decision = decision; a.actor = actor; a.decided_at = datetime.now(timezone.utc)
    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        return [a.model_copy(deep=True) for a in self.approvals.values()
                if a.target == target and a.gate == gate
                and (decision is None or a.decision == decision)]
    def emit_event(self, event: Event) -> None:
        self.events.append(event)

# ACT clamps list endpoints at 500; request the max so nothing is silently dropped.
_LIST_LIMIT = 500

# ACT's severity vocabulary uses "warn"; Navigator's enum uses "warning".
_SEVERITY_TO_ACT = {"warning": "warn"}


class HTTPACTClient:
    """REST client for ACT's /api/v1/agent resource (agent-resource patch, applied
    2026-07-02, plus the HTTP-client support routes). Navigator-only fields that
    have no ACT column (title, rationale, intended_changes, affected_files,
    checkpoints, ...) round-trip through the JSON payload column. Navigator-minted
    ids are sent on create and are the identity (ACT 409s on conflict)."""

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = f"{base_url.rstrip('/')}/api/v1/agent"
        # local ACT can stall past httpx's 5s default (live ReadTimeout 2026-07-15)
        self.client = client or httpx.Client(timeout=30.0)

    # ---- proposals ----------------------------------------------------------

    def create_proposal(self, p: Proposal) -> Proposal:
        r = self.client.post(f"{self.base_url}/proposals", json={
            "id": p.id, "schema_version": p.schema_version, "origin": p.origin,
            "proposal_origin_hash": p.proposal_origin_hash, "repo": p.repo,
            "base_commit_sha": p.base_commit_sha, "risk_tier": p.risk_tier.value,
            "priority": p.priority, "payload": {
                "title": p.title, "rationale": p.rationale,
                "intended_changes": p.intended_changes,
                "affected_files": p.affected_files,
                "supersedes_proposal_id": p.supersedes_proposal_id,
            }})
        r.raise_for_status()
        return self._proposal_from(r.json())

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        r = self.client.get(f"{self.base_url}/proposals/{proposal_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._proposal_from(r.json())

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        params: dict = {"limit": _LIST_LIMIT}
        if status is not None:
            params["status"] = status.value
        r = self.client.get(f"{self.base_url}/proposals", params=params)
        r.raise_for_status()
        return [self._proposal_from(d) for d in r.json()]

    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.client.post(f"{self.base_url}/proposals/{proposal_id}/status",
                         json={"status": status.value}).raise_for_status()

    @staticmethod
    def _proposal_from(d: dict) -> Proposal:
        pl = d.get("payload") or {}
        return Proposal(
            id=d["id"], schema_version=d["schema_version"], priority=d["priority"],
            origin=d["origin"], proposal_origin_hash=d["proposal_origin_hash"],
            supersedes_proposal_id=pl.get("supersedes_proposal_id"),
            title=pl.get("title", ""), rationale=pl.get("rationale", ""),
            repo=d["repo"], base_commit_sha=d["base_commit_sha"],
            intended_changes=pl.get("intended_changes", ""),
            affected_files=pl.get("affected_files") or [],
            risk_tier=d["risk_tier"], created_at=d["created_at"], status=d["status"])

    # ---- runs ----------------------------------------------------------------

    def create_run(self, r: Run) -> Run:
        resp = self.client.post(f"{self.base_url}/runs", json={
            "id": r.id, "schema_version": r.schema_version, "proposal_id": r.proposal_id,
            "phase": r.phase.value, "base_commit_sha": r.base_commit_sha,
            "branch": r.branch, "worktree_path": r.worktree_path,
            "correlation_id": r.correlation_id, "cost_usd": r.cost_usd,
            "payload": self._run_payload(r)})
        resp.raise_for_status()
        return self._run_from(resp.json())

    def update_run(self, r: Run) -> None:
        self.client.post(f"{self.base_url}/runs/{r.id}", json={
            "status": r.status.value, "phase": r.phase.value,
            "worktree_path": r.worktree_path,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "cost_usd": r.cost_usd,
            "payload": self._run_payload(r)}).raise_for_status()

    def list_open_runs(self) -> list[Run]:
        r = self.client.get(f"{self.base_url}/runs", params={"limit": _LIST_LIMIT})
        r.raise_for_status()
        return [self._run_from(d) for d in r.json()]

    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]:
        r = self.client.get(f"{self.base_url}/runs", params={
            "proposal_id": proposal_id, "open_only": "false", "limit": _LIST_LIMIT})
        r.raise_for_status()
        return [self._run_from(d) for d in r.json()]

    @staticmethod
    def _run_payload(r: Run) -> dict:
        return {"completed_tasks": r.completed_tasks, "target_branch": r.target_branch,
                "verification": r.verification, "model_calls": r.model_calls,
                "checkpoints": [c.model_dump() for c in r.checkpoints],
                "heartbeat_at": r.heartbeat_at.isoformat()}

    @staticmethod
    def _run_from(d: dict) -> Run:
        pl = d.get("payload") or {}
        return Run(
            id=d["id"], schema_version=d["schema_version"], proposal_id=d["proposal_id"],
            status=d["status"], phase=d["phase"],
            heartbeat_at=pl.get("heartbeat_at") or d["started_at"],
            worktree_path=d.get("worktree_path"), base_commit_sha=d["base_commit_sha"],
            branch=d["branch"], target_branch=pl.get("target_branch"),
            verification=pl.get("verification") or [],
            model_calls=pl.get("model_calls") or 0,
            completed_tasks=pl.get("completed_tasks") or [],
            checkpoints=pl.get("checkpoints") or [], cost_usd=d.get("cost_usd", 0.0),
            correlation_id=d.get("correlation_id"), started_at=d["started_at"],
            ended_at=d.get("ended_at"))

    # ---- approvals -----------------------------------------------------------

    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        r = self.client.post(f"{self.base_url}/approvals",
                             json={"target": target, "gate": gate.value})
        r.raise_for_status()
        return self._approval_from(r.json())

    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str) -> None:
        self.client.post(f"{self.base_url}/approvals/{approval_id}/decide",
                         json={"decision": decision.value, "actor": actor}).raise_for_status()

    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        params: dict = {"target": target, "gate": gate.value, "limit": _LIST_LIMIT}
        if decision is not None:
            params["decision"] = decision.value
        r = self.client.get(f"{self.base_url}/approvals", params=params)
        r.raise_for_status()
        return [self._approval_from(d) for d in r.json()]

    @staticmethod
    def _approval_from(d: dict) -> Approval:
        return Approval(
            id=d["id"], schema_version=d["schema_version"], target=d["target"],
            gate=d["gate"], decision=d["decision"], actor=d.get("actor"),
            decided_at=d.get("decided_at"), note=d.get("note"),
            correlation_id=d.get("correlation_id"))

    # ---- events ---------------------------------------------------------------

    def emit_event(self, event: Event) -> None:
        self.client.post(f"{self.base_url}/events", json={
            "kind": event.kind,
            "severity": _SEVERITY_TO_ACT.get(event.severity.value, event.severity.value),
            "correlation_id": event.correlation_id,
            "payload": event.payload}).raise_for_status()
