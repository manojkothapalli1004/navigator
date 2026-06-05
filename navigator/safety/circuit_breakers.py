import time
from collections import Counter
from navigator.safety.errors import BreakerTripped

class CircuitBreakers:
    def __init__(self, max_tool_failures: int, max_identical_failures: int,
                 max_run_duration_s: int, max_failed_tests: int):
        self.max_tool_failures = max_tool_failures
        self.max_identical_failures = max_identical_failures
        self.max_run_duration_s = max_run_duration_s
        self.max_failed_tests = max_failed_tests
        self._failures = 0
        self._identical = Counter()

    def record_failure(self, signature: str) -> None:
        self._failures += 1
        self._identical[signature] += 1
        if self._failures >= self.max_tool_failures:
            raise BreakerTripped(f"max_tool_failures={self.max_tool_failures}")
        if self._identical[signature] >= self.max_identical_failures:
            raise BreakerTripped(f"max_identical_failures for {signature!r}")

    def check_duration(self, started_monotonic: float) -> None:
        if time.monotonic() - started_monotonic >= self.max_run_duration_s:
            raise BreakerTripped("max_run_duration")

    def check_failed_tests(self, n: int) -> None:
        if n >= self.max_failed_tests:
            raise BreakerTripped("max_failed_tests")
