from datetime import datetime
from pydantic import BaseModel, Field
from navigator.ids import new_id
from navigator.contracts.enums import ApprovalGate, ApprovalDecision

class Approval(BaseModel):
    id: str = Field(default_factory=lambda: new_id("approval"))
    schema_version: int = 1
    target: str
    gate: ApprovalGate
    decision: ApprovalDecision = ApprovalDecision.pending
    actor: str | None = None
    decided_at: datetime | None = None
    note: str | None = None
    correlation_id: str | None = None
