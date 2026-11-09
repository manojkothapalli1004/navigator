"""Credits fallback in make_sdk_query_fn: limit -> one retry on the API key; key never leaks."""
import json
import logging
import sys
import types

import pytest

from navigator import credits
from navigator.runtime.drivers.sdk_query import make_sdk_query_fn

KEY = "sk-ant-TEST-KEY"
GOOD = '{"commands": [], "summary": "ok"}'
LIMIT = "You've hit your limit · resets 4:20am"
FABLE, OPUS = "claude-fable-5-1", "claude-opus-5-5"


@pytest.fixture
def cenv(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text(f'ANTHROPIC_API_KEY="{KEY}"\n')
    st = tmp_path / "credits.json"
    monkeypatch.setenv("NAVIGATOR_CREDITS_STATE", str(st))
    monkeypatch.setenv("NAVIGATOR_CREDITS_ENV_FILE", str(envf))
    for k in ("CREDITS_FALLBACK", "CREDITS_DAILY_CAP_USD", "CREDITS_FLOOR_USD",
              "CREDITS_START_BALANCE_USD", "CREDITS_EXPIRES", "CREDITS_CALL_RESERVE_USD",
              "CREDITS_CALL_MAX_USD", "CREDITS_PROBE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAVIGATOR_CREDITS_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("limited", "stub"), raising=False)
    credits.set_notifier(None)
    st.write_text(json.dumps({"verified": True, "est_balance": 83.58}))
    yield types.SimpleNamespace(state=st)
    credits.set_notifier(None)


def _install(monkeypatch, steps):
    """steps: per call, ("ok", text) | ("error", text). Records env and model per call."""
    mod = types.ModuleType("claude_agent_sdk")
    calls = []

    class ResultMessage:
        def __init__(self, result=None, is_error=False, subtype="success"):
            self.result, self.is_error = result, is_error
            self.total_cost_usd, self.usage = 0.5, None
            self.num_turns, self.subtype = 1, subtype

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

        def __repr__(self):
            return "OPTIONS"

    async def query(*, prompt, options):
        calls.append({"env": options.kw.get("env"), "model": options.kw.get("model"),
                      "budget": options.kw.get("max_budget_usd", "absent")})
        kind, text = steps[min(len(calls), len(steps)) - 1]
        if kind == "budget":
            yield ResultMessage(result=None, is_error=True, subtype="error_max_budget_usd")
            raise Exception("error result: error_max_budget_usd")
        if kind == "crash":  # no ResultMessage at all; "not logged in" keeps it non-retryable
            raise Exception("crash: not logged in")
        yield ResultMessage(result=text, is_error=kind == "error")
        if kind == "error":
            raise Exception("error result: success")

    mod.query, mod.ClaudeAgentOptions, mod.ResultMessage = query, ClaudeAgentOptions, ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return calls


def _st(cenv):
    return json.loads(cenv.state.read_text())


def test_normal_call_has_no_key_env(cenv, monkeypatch):
    calls = _install(monkeypatch, [("ok", GOOD)])
    assert make_sdk_query_fn()("p") == GOOD
    assert not calls[0]["env"]


def test_limit_retries_once_with_key_and_records(cenv, monkeypatch):
    calls = _install(monkeypatch, [("error", LIMIT), ("ok", GOOD)])
    notes = []
    credits.set_notifier(notes.append)
    assert make_sdk_query_fn()("p") == GOOD
    assert len(calls) == 2 and not calls[0]["env"]
    assert calls[1]["env"]["ANTHROPIC_API_KEY"] == KEY
    s = _st(cenv)
    assert s["reserved"] == [] and s["spent_today"] > 0
    assert notes


def test_credits_call_carries_budget_and_plain_call_has_none(cenv, monkeypatch):
    calls = _install(monkeypatch, [("error", LIMIT), ("ok", GOOD)])
    assert make_sdk_query_fn()("p") == GOOD
    assert calls[0]["budget"] == "absent" and calls[1]["budget"] == round(5.0 / 1.1, 4)


def test_next_query_routes_to_credits_while_window_open(cenv, monkeypatch):
    calls = _install(monkeypatch, [("error", LIMIT), ("ok", GOOD), ("ok", GOOD)])
    fn = make_sdk_query_fn()
    fn("p"); fn("p")
    assert len(calls) == 3
    assert calls[2]["env"]["ANTHROPIC_API_KEY"] == KEY


def test_unverified_limit_raises_without_retry(cenv, monkeypatch):
    cenv.state.write_text(json.dumps({"verified": False}))
    calls = _install(monkeypatch, [("error", LIMIT), ("ok", GOOD)])
    with pytest.raises(RuntimeError):
        make_sdk_query_fn()("p")
    assert len(calls) == 1


def test_retry_failing_releases_reservation(cenv, monkeypatch):
    _install(monkeypatch, [("error", LIMIT), ("error", "boom")])
    with pytest.raises(RuntimeError):
        make_sdk_query_fn()("p")
    s = _st(cenv)
    assert s["reserved"] == [] and s["spent_today"] > 0      # each attempt's result was recorded


def test_launched_key_call_without_result_is_charged_its_hold(cenv, monkeypatch):
    # the CLI crashed after launch with no ResultMessage: charge the hold (the most it could have spent)
    _install(monkeypatch, [("error", LIMIT), ("crash", "")])
    with pytest.raises(Exception, match="crash"):      # the fake SDK's bare exception propagates
        make_sdk_query_fn()("p")
    s = _st(cenv)
    assert s["reserved"] == [] and abs(s["spent_today"] - 5.0 * 1.1) < 1e-9


def test_budget_stop_is_final_and_explained(cenv, monkeypatch):
    calls = _install(monkeypatch, [("error", LIMIT), ("budget", "")])
    with pytest.raises(RuntimeError, match="max_budget"):
        make_sdk_query_fn()("p")
    assert len(calls) == 2                       # no third attempt on a fresh budget


def test_model_scoped_error_falls_back_model_first_without_credits(cenv, monkeypatch):
    calls = _install(monkeypatch, [("error", "Usage credits are required for this model."),
                                   ("ok", GOOD)])
    out = make_sdk_query_fn(model=FABLE, fallbacks=(OPUS,))("p")
    assert out == GOOD
    assert [c["model"] for c in calls] == [FABLE, OPUS]
    assert not calls[1]["env"] and _st(cenv).get("until", 0) == 0


def test_credit_balance_error_on_credits_call_marks_exhausted(cenv, monkeypatch):
    notes = []
    credits.set_notifier(notes.append)
    _install(monkeypatch, [("error", LIMIT), ("error", "Credit balance is too low")])
    with pytest.raises(RuntimeError):
        make_sdk_query_fn()("p")
    s = _st(cenv)
    assert s["exhausted"] is True and s["reserved"] == []


def test_error_text_is_redacted(cenv, monkeypatch):
    _install(monkeypatch, [("error", LIMIT), ("error", f"bad {KEY} echoed")])
    with pytest.raises(RuntimeError) as ei:
        make_sdk_query_fn()("p")
    assert KEY not in str(ei.value) and "[redacted]" in str(ei.value)


def test_key_never_logged(cenv, monkeypatch, caplog, capsys):
    from navigator.observability.logging import get_logger  # noqa: F401
    _install(monkeypatch, [("error", LIMIT), ("error", f"bad {KEY} echoed")])
    seen = []

    class L:
        def info(self, ev, **kw): seen.append((ev, kw))
        warning = info

    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError):
        make_sdk_query_fn(logger=L())("p")
    out = capsys.readouterr()
    assert KEY not in repr(seen) and KEY not in caplog.text + out.out + out.err


def test_rate_limit_event_triggers_retry(cenv, monkeypatch):
    # event-only limit: error text says nothing, the RateLimitEvent is rejected/five_hour
    mod = types.ModuleType("claude_agent_sdk")
    calls = []

    class ResultMessage:
        def __init__(self, result=None, is_error=False):
            self.result, self.is_error, self.total_cost_usd = result, is_error, 0.1
            self.usage, self.num_turns, self.subtype = None, 1, "success"

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    async def query(*, prompt, options):
        calls.append(options.kw.get("env"))
        if len(calls) == 1:
            yield types.SimpleNamespace(rate_limit_info=types.SimpleNamespace(
                status="rejected", rate_limit_type="five_hour", resets_at=None))
            yield ResultMessage(result="Request rejected", is_error=True)
            raise Exception("error result: success")
        yield ResultMessage(result=GOOD)

    mod.query, mod.ClaudeAgentOptions, mod.ResultMessage = query, ClaudeAgentOptions, ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    assert make_sdk_query_fn()("p") == GOOD
    assert calls[1]["ANTHROPIC_API_KEY"] == KEY
