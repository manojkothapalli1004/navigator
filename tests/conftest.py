"""Shared pytest configuration.

Test isolation for structlog: navigator.__main__._configure_logging() (exercised
by tests/test_main.py) binds structlog's logger factory to the stream that was
sys.stderr at configure time, which under pytest is a per-test capture stream.
That stream is closed when the test ends, so any later test that logs would
crash with 'ValueError: I/O operation on closed file'. Resetting structlog to
its defaults after every test keeps global logging state from leaking.
"""
import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog_after_test():
    yield
    structlog.reset_defaults()
