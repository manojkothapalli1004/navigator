import hashlib, json
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from navigator.ids import new_id
from navigator.contracts.enums import ProposalStatus, RiskTier

def compute_origin_hash(project: str, normalized_intent: str, affected_files: list[str],
                        trigger_kind: str, source_event_id: str | None) -> str:
    material = json.dumps({
        "project": project,
        "normalized_intent": normalized_intent.strip().lower(),
        "affected_files": sorted(affected_files),
        "trigger_kind": trigger_kind,
        "source_event_id": source_event_id,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()

class RepoSnapshot(BaseModel):
    base_commit_sha: str

class Proposal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("proposal"))
    schema_version: int = 1
    priority: int = 5
    origin: str
    proposal_origin_hash: str
    supersedes_proposal_id: str | None = None
    title: str
    rationale: str
    repo: str
    base_commit_sha: str
    intended_changes: str
    affected_files: list[str] = Field(default_factory=list)
    risk_tier: RiskTier = RiskTier.low
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: ProposalStatus = ProposalStatus.proposed
