from navigator.safety.errors import CommandRejected, BreakerTripped, BudgetExceeded, GateNotImplemented

def test_errors_are_exceptions():
    for exc in (CommandRejected, BreakerTripped, BudgetExceeded, GateNotImplemented):
        assert issubclass(exc, Exception)
