from navigator.contracts.event import Event
from navigator.contracts.enums import Severity

def test_event_shape_includes_severity_and_correlation():
    e = Event(source="navigator", kind="run.phase_changed", severity=Severity.info,
              correlation_id="corr-1", agent_id="navigator", payload={"phase": "building"})
    assert e.id.startswith("event-")
    assert e.source == "navigator"
    assert e.severity == Severity.info
    assert e.schema_version == 1
    assert e.ts is not None
