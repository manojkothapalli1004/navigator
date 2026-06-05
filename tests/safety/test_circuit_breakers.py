import pytest
from navigator.safety.circuit_breakers import CircuitBreakers
from navigator.safety.errors import BreakerTripped

def test_max_tool_failures_trips():
    cb = CircuitBreakers(max_tool_failures=2, max_identical_failures=99,
                         max_run_duration_s=999, max_failed_tests=99)
    cb.record_failure("a");
    with pytest.raises(BreakerTripped):
        cb.record_failure("b")

def test_identical_failures_trip():
    cb = CircuitBreakers(max_tool_failures=99, max_identical_failures=2,
                         max_run_duration_s=999, max_failed_tests=99)
    cb.record_failure("same")
    with pytest.raises(BreakerTripped):
        cb.record_failure("same")

def test_duration_trips():
    cb = CircuitBreakers(max_tool_failures=99, max_identical_failures=99,
                         max_run_duration_s=0, max_failed_tests=99)
    with pytest.raises(BreakerTripped):
        cb.check_duration(started_monotonic=-1.0)
