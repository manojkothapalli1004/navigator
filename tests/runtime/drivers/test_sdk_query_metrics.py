"""Metrics events from make_sdk_query_fn.

These tests inject a recording fake logger via logger=... and a fake
claude_agent_sdk module via monkeypatch. They never configure structlog or
call navigator.__main__._configure_logging: global logging state must not
leak into other tests.
"""
import asyncio
import sys
import types

import pytest

from navigator.runtime.drivers.sdk_query import make_sdk_query_fn


class _ResultMessage:
    def __init__(self, result=None, is_error=False, **extra):
        self.result = result
        self.is_error = is_error
        for k, v in extra.items():
            setattr(self, k, v)


class _Options:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_sdk(monkeypatch, messages=None, stall=False):
    async def query(prompt, options):
        if stall:
            await asyncio.sleep(10)
        for m in messages or []:
            yield m

    mod = types.ModuleType("claude_agent_sdk")
    mod.ClaudeAgentOptions = _Options
    mod.ResultMessage = _ResultMessage
    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)


class _RecordingLogger:
    def __init__(self):
        self.events = []

    def info(self, event, **fields):
        self.events.append(("info", event, fields))

    def warning(self, event, **fields):
        self.events.append(("warning", event, fields))


class _RaisingLogger:
    def info(self, event, **fields):
        raise RuntimeError("logger broken")

    def warning(self, event, **fields):
        raise RuntimeError("logger broken")


def test_completed_event_fields(monkeypatch, capsys):
    _install_fake_sdk(monkeypatch, [_ResultMessage(result='{"a": 1}', num_turns=2,
                                                   subtype="success")])
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model="m-1", logger=log)
    assert fn("hi") == '{"a": 1}'
    assert len(log.events) == 1
    level, event, fields = log.events[0]
    assert (level, event) == ("info", "sdk_query.completed")
    assert fields["model"] == "m-1"
    assert fields["num_turns"] == 2
    assert fields["subtype"] == "success"
    assert fields["reply_chars"] == len('{"a": 1}')
    assert isinstance(fields["elapsed_s"], float) and fields["elapsed_s"] >= 0
    assert "timeout_s" not in fields
    assert capsys.readouterr().out == ""


def test_missing_result_attributes_are_none(monkeypatch):
    _install_fake_sdk(monkeypatch, [_ResultMessage(result='{"a": 1}')])
    log = _RecordingLogger()
    make_sdk_query_fn(logger=log)("hi")
    _, _, fields = log.events[0]
    assert fields["num_turns"] is None and fields["subtype"] is None


def test_failed_event_on_error_result(monkeypatch, capsys):
    # non-retryable error: exactly one query, one failed event
    _install_fake_sdk(monkeypatch, [_ResultMessage(result="usage limit reached", is_error=True,
                                                   num_turns=1, subtype="error_during_execution")])
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model="m-1", logger=log)
    with pytest.raises(RuntimeError, match="usage limit"):
        fn("hi")
    assert len(log.events) == 1
    level, event, fields = log.events[0]
    assert (level, event) == ("warning", "sdk_query.failed")
    assert fields["model"] == "m-1"
    assert fields["num_turns"] == 1
    assert fields["subtype"] == "error_during_execution"
    assert fields["reply_chars"] == 0
    assert "timeout_s" not in fields
    assert capsys.readouterr().out == ""


def test_timeout_event_includes_timeout_s(monkeypatch, capsys):
    _install_fake_sdk(monkeypatch, stall=True)
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model="m-1", timeout_s=0.05, logger=log)
    with pytest.raises(TimeoutError):
        fn("hi")
    assert len(log.events) == 1  # timeouts are not retried
    level, event, fields = log.events[0]
    assert (level, event) == ("warning", "sdk_query.failed")
    assert fields["timeout_s"] == 0.05
    assert fields["elapsed_s"] >= 0
    assert capsys.readouterr().out == ""


def test_raising_logger_does_not_change_result(monkeypatch, capsys):
    _install_fake_sdk(monkeypatch, [_ResultMessage(result='{"ok": true}')])
    fn = make_sdk_query_fn(logger=_RaisingLogger())
    assert fn("hi") == '{"ok": true}'
    assert capsys.readouterr().out == ""


def test_raising_logger_does_not_change_exception(monkeypatch):
    _install_fake_sdk(monkeypatch, [_ResultMessage(result="not logged in", is_error=True)])
    fn = make_sdk_query_fn(logger=_RaisingLogger())
    with pytest.raises(RuntimeError, match="not logged in"):
        fn("hi")
