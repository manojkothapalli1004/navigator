from datetime import datetime, timezone
from pydantic import BaseModel, Field
from navigator.ids import new_id
from navigator.contracts.enums import Severity

def _now() -> datetime:
    return datetime.now(timezone.utc)

class Event(BaseModel):
    id: str = Field(default_factory=lambda: new_id("event"))
    schema_version: int = 1
    source: str = "navigator"
    kind: str
    severity: Severity = Severity.info
    ts: datetime = Field(default_factory=_now)
    correlation_id: str | None = None
    agent_id: str | None = "navigator"
    payload: dict = Field(default_factory=dict)
