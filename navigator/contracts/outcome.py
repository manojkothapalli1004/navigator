from pydantic import BaseModel, Field
from navigator.ids import new_id

class Rollback(BaseModel):
    checkpoints_made: int = 0
    checkpoints_restored: int = 0
    worktree_preserved: bool = False
    preserved_path: str | None = None

class Outcome(BaseModel):
    id: str = Field(default_factory=lambda: new_id("outcome"))
    schema_version: int = 1
    run_id: str
    proposal_id: str | None = None
    status: str
    summary: str
    lessons: list[str] = Field(default_factory=list)
    diff_ref: str | None = None
    verification_notes: str | None = None
    provenance: str | None = None
    rollback: Rollback = Field(default_factory=Rollback)
    # git HEAD of the navigator code that produced this outcome; None when unknown
    # (old JSONL lines, or git unavailable)
    code_sha: str | None = None
