from datetime import datetime, timezone
from pydantic import BaseModel, Field
from navigator.ids import new_id
from navigator.contracts.enums import RunStatus, RunPhase

class Checkpoint(BaseModel):
    sha: str
    label: str

class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    schema_version: int = 1
    proposal_id: str
    status: RunStatus = RunStatus.executing
    phase: RunPhase = RunPhase.pending
    heartbeat_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    worktree_path: str | None = None
    base_commit_sha: str
    branch: str
    target_branch: str | None = None
    completed_tasks: list[str] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    verification: list[dict] = Field(default_factory=list)
    cost_usd: float = 0.0  # budget-cap estimate, not a bill (runs on the subscription)
    model_calls: int = 0
    correlation_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
