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


@pytest.fixture(autouse=True)
def _isolate_credits_ledger(tmp_path, monkeypatch):
    """No test may touch the live credits ledger, key file or events log, and no test may run the
    live subscription probe. 2026-11-09: a full-suite run exercised the limit path through
    sdk_query -> credits.on_limit() against the REAL ledger (only three fixtures redirected it),
    opened a credits window, and the live conductor then spent on the API key."""
    from navigator import credits
    monkeypatch.setenv("NAVIGATOR_CREDITS_STATE", str(tmp_path / "_credits_state.json"))
    monkeypatch.setenv("NAVIGATOR_CREDITS_ENV_FILE", str(tmp_path / "_credits.env"))
    monkeypatch.setenv("NAVIGATOR_CREDITS_LOG", str(tmp_path / "_credits_events.jsonl"))
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("limited", "conftest stub"),
                        raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_repo_memory(tmp_path, monkeypatch):
    """No test may write the live per-repo memory log (reviewer memory B1, the same lesson): the
    switch is off unless a test turns it on, the env root points under tmp, AND the module default
    (what repo_memory.root_for() falls back to when neither Settings nor the env names a root)
    points under tmp too - a test running Settings(repo_memory=True) without a root must still land
    in tmp, never in /Users/Manoj/calwbot/logs/navigator/repo_memory."""
    from navigator.runtime import repo_memory
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY", raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "_repo_memory"))
    monkeypatch.setattr(repo_memory, "DEFAULT_ROOT", str(tmp_path / "_repo_memory_default"))
    yield
