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


# --- model fallback events (operator decision 2026-10-15) ---------------------------

FABLE, OPUS, SONNET = "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"


def _install_model_fake_sdk(monkeypatch, by_model):
    """Per-model fake: by_model maps the model named in the options to its messages.
    Returns the list of models queried, in order."""
    calls = []

    async def query(prompt, options):
        model = options.kwargs["model"]
        calls.append(model)
        for m in by_model[model]:
            yield m

    mod = types.ModuleType("claude_agent_sdk")
    mod.ClaudeAgentOptions = _Options
    mod.ResultMessage = _ResultMessage
    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return calls


def test_fallback_emits_failed_per_model_plus_one_marker_only_fallback_event(monkeypatch, capsys):
    calls = _install_model_fake_sdk(monkeypatch, {
        FABLE: [_ResultMessage(result="Usage credits are required for this model.", is_error=True,
                               num_turns=1, subtype="error_during_execution")],
        OPUS: [_ResultMessage(result='{"a": 1}', num_turns=1, subtype="success")]})
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model=FABLE, fallbacks=(OPUS, SONNET), logger=log)
    assert fn("hi") == '{"a": 1}'
    assert calls == [FABLE, OPUS]
    assert [(lvl, ev) for lvl, ev, _ in log.events] == [
        ("warning", "sdk_query.failed"), ("warning", "sdk_query.model_fallback"),
        ("info", "sdk_query.completed")]
    failed, fallback, completed = [f for _, _, f in log.events]
    assert failed["model"] == FABLE and "Usage credits" in failed["error"]
    assert fallback == {"from_model": FABLE, "to_model": OPUS, "reason": "usage credits"}
    assert completed["model"] == OPUS  # the model actually used is what gets recorded
    assert capsys.readouterr().out == ""


def test_default_fallbacks_disabled_keeps_main_retry_for_credit_balance(monkeypatch):
    # 'credit balance' is a fallback marker but not a _NON_RETRYABLE one: with
    # fallbacks=() the fn must behave exactly as on main (one bounded retry on the
    # same model, two failed events, no fallback event)
    calls = _install_model_fake_sdk(monkeypatch, {
        "m": [_ResultMessage(result="Your credit balance is too low", is_error=True)]})
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model="m", fallbacks=(), logger=log)
    with pytest.raises(RuntimeError, match="credit balance"):
        fn("hi")
    assert calls == ["m", "m"]
    assert [ev for _, ev, _ in log.events] == ["sdk_query.failed", "sdk_query.failed"]
    assert all(f["model"] == "m" for _, _, f in log.events)


def test_exhausted_chain_emits_failed_for_every_model_and_raises_last_error(monkeypatch):
    calls = _install_model_fake_sdk(monkeypatch, {
        FABLE: [_ResultMessage(result="Usage credits are required", is_error=True)],
        OPUS: [_ResultMessage(result="You've hit your usage limit", is_error=True)],
        SONNET: [_ResultMessage(result="You've hit your session limit", is_error=True)]})
    log = _RecordingLogger()
    fn = make_sdk_query_fn(model=FABLE, fallbacks=(OPUS, SONNET), logger=log)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("hi")
    assert calls == [FABLE, OPUS, SONNET]
    events = [(ev, f.get("model"), f.get("to_model")) for _, ev, f in log.events]
    assert events == [("sdk_query.failed", FABLE, None), ("sdk_query.model_fallback", None, OPUS),
                      ("sdk_query.failed", OPUS, None), ("sdk_query.model_fallback", None, SONNET),
                      ("sdk_query.failed", SONNET, None),
                      ("sdk_query.account_cooldown", SONNET, None)]
    for _, ev, f in log.events:
        if ev == "sdk_query.model_fallback":
            assert set(f) == {"from_model", "to_model", "reason"}
            assert "You've hit" not in str(f) and "Usage credits are" not in str(f)


def test_raising_logger_does_not_change_fallback_result(monkeypatch):
    _install_model_fake_sdk(monkeypatch, {
        FABLE: [_ResultMessage(result="Usage credits are required", is_error=True)],
        OPUS: [_ResultMessage(result='{"ok": true}')]})
    fn = make_sdk_query_fn(model=FABLE, fallbacks=(OPUS,), logger=_RaisingLogger())
    assert fn("hi") == '{"ok": true}'
