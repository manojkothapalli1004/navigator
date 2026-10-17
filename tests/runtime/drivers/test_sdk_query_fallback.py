"""Model fallback chain in make_sdk_query_fn (operator decision 2026-10-15).

Fake claude_agent_sdk, no network, no sleep: the fake answers per model named in the
options, so every test states exactly which models were called and in what order.
"""
import asyncio
import re
import sys
import types

import pytest

from navigator.runtime.drivers.sdk_query import make_sdk_query_fn, model_chain

FABLE, OPUS, SONNET = "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"
CHAIN = (OPUS, SONNET)
GOOD = '{"commands": [], "summary": "ok"}'
CREDITS = "Usage credits are required for this model."
MAX_TURNS = "Reached maximum number of turns (4)"
SESSION = "You've hit your session limit \u00b7 resets 10:30pm"
ALL_COOLING = "all models cooling down until"


def _install(monkeypatch, behaviour, stall=None):
    """behaviour: model -> steps; the n-th call on a model takes its n-th step (the last
    step repeats). A step is ("ok", text), ("error", text) (an is_error result followed
    by the SDK's generic exception, as the real CLI does) or an Exception to raise.
    `stall` names a model whose query never returns (timeout path)."""
    mod = types.ModuleType("claude_agent_sdk")

    class ResultMessage:
        def __init__(self, result=None, is_error=False):
            self.result = result
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kwargs = kw

    calls: list = []

    async def query(*, prompt, options):
        model = options.kwargs["model"]
        calls.append(model)
        if stall is not None and model == stall:
            await asyncio.sleep(3600)
        steps = behaviour[model]
        step = steps[min(calls.count(model) - 1, len(steps) - 1)]
        if isinstance(step, Exception):
            raise step
        kind, text = step
        if kind == "error":
            yield ResultMessage(result=text, is_error=True)
            raise Exception("Claude Code returned an error result: success")
        yield ResultMessage(result=text)

    mod.query = query
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return calls


class _Log:
    def __init__(self):
        self.events = []

    def info(self, event, **fields):
        self.events.append((event, fields))

    def warning(self, event, **fields):
        self.events.append((event, fields))

    def fallbacks(self):
        return [f for ev, f in self.events if ev == "sdk_query.model_fallback"]

    def kinds(self):
        return [ev for ev, _ in self.events]


def _fn(model=FABLE, **kw):
    kw.setdefault("fallbacks", CHAIN)
    kw.setdefault("logger", _Log())
    return make_sdk_query_fn(model=model, **kw)


def test_model_chain_never_falls_back_up_and_deduplicates():
    # rev5 definition: first occurrence wins, empty entries dropped, and a model that is
    # itself in the chain only tries the models AFTER it
    assert model_chain(FABLE, (OPUS, OPUS, "", FABLE, SONNET, OPUS)) == [FABLE, SONNET]
    assert model_chain(FABLE, CHAIN) == [FABLE, OPUS, SONNET]
    assert model_chain(OPUS, CHAIN) == [OPUS, SONNET]
    assert model_chain(SONNET, CHAIN) == [SONNET]
    assert model_chain(FABLE, ()) == [FABLE]
    assert model_chain(FABLE, ("", "")) == [FABLE]
    assert model_chain("", CHAIN) == [""]
    assert model_chain(None, CHAIN) == [None]


def test_fable_credits_error_falls_back_to_opus_with_one_call_each(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)], OPUS: [("ok", GOOD)]})
    log = _Log()
    assert _fn(logger=log)("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert log.fallbacks() == [{"from_model": FABLE, "to_model": OPUS, "reason": "usage credits"}]
    # the event carries the marker only, never the error text
    assert CREDITS not in str(log.fallbacks())


def test_fable_and_opus_credits_errors_fall_back_to_sonnet(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)],
                                   OPUS: [("error", "Your credit balance is too low")],
                                   SONNET: [("ok", GOOD)]})
    log = _Log()
    assert _fn(logger=log)("x") == GOOD
    assert calls == [FABLE, OPUS, SONNET]
    assert [(f["from_model"], f["to_model"], f["reason"]) for f in log.fallbacks()] == [
        (FABLE, OPUS, "usage credits"), (OPUS, SONNET, "credit balance")]


def test_every_model_failing_raises_the_last_error_unchanged(monkeypatch):
    class _Billing(Exception):
        pass

    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)],
                                   OPUS: [("error", "You've hit your usage limit")],
                                   SONNET: [_Billing("usage credits exhausted on sonnet")]})
    cooldowns = {}
    with pytest.raises(_Billing, match="sonnet"):
        _fn(cooldowns=cooldowns)("x")
    assert calls == [FABLE, OPUS, SONNET]
    assert set(cooldowns) == {FABLE, OPUS, SONNET}  # every failed model cools down


def test_error_result_exhaustion_raises_runtime_error_from_the_last_model(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)],
                                   OPUS: [("error", CREDITS)],
                                   SONNET: [("error", "You've hit your session limit")]})
    with pytest.raises(RuntimeError, match="session limit"):
        _fn()("x")
    assert calls == [FABLE, OPUS, SONNET]


def test_cooldown_skips_the_failed_model_until_it_expires(monkeypatch):
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS), ("ok", GOOD)], OPUS: [("ok", GOOD)]})
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (3600.0, False)}  # model-specific entry
    now["t"] = 100.0  # within COOLDOWN_S: start directly at opus
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, OPUS]
    now["t"] = 3601.0  # expired: fable is tried again
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, OPUS, FABLE]


def test_cooldown_dict_shared_between_query_fns_skips_the_model_everywhere(monkeypatch):
    # the builder's exhausted model must be skipped by any other fn sharing the dict
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)], OPUS: [("ok", GOOD)]})
    now = {"t": 0.0}
    cooldowns = {}
    builder = _fn(cooldowns=cooldowns, clock=lambda: now["t"])
    other = _fn(cooldowns=cooldowns, clock=lambda: now["t"])
    builder("x")
    now["t"] = 1.0
    other("x")
    assert calls == [FABLE, OPUS, OPUS]


def test_all_models_cooling_fails_fast_without_an_sdk_call(monkeypatch):
    # rev2: re-querying a chain known to be cooling burned three failing calls per tick;
    # bare-float entries written by older callers are honoured too
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)]})
    cooldowns = {FABLE: 1e9, OPUS: (1e9, False), SONNET: (1e9, True)}
    log = _Log()
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 0.0, logger=log)("x")
    assert calls == []
    assert re.search(r"until \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(exc.value))
    assert log.events == []  # no query: no sdk_query.* event either


def test_planner_on_sonnet_never_calls_opus_or_fable(monkeypatch):
    calls = _install(monkeypatch, {SONNET: [("error", CREDITS)]})
    log = _Log()
    with pytest.raises(RuntimeError, match="Usage credits"):
        _fn(model=SONNET, logger=log)("x")
    assert calls == [SONNET]
    assert log.fallbacks() == []


def test_opus_only_falls_back_down_to_sonnet(monkeypatch):
    calls = _install(monkeypatch, {OPUS: [("error", CREDITS)], SONNET: [("ok", GOOD)]})
    assert _fn(model=OPUS)("x") == GOOD
    assert calls == [OPUS, SONNET]


def test_not_logged_in_never_falls_back(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", "not logged in")], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    with pytest.raises(RuntimeError, match="not logged in"):
        _fn(cooldowns=cooldowns, logger=log)("x")
    assert calls == [FABLE]
    assert cooldowns == {} and log.fallbacks() == []


def test_ordinary_error_result_is_still_retried_on_the_same_model(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", MAX_TURNS), ("ok", GOOD)], OPUS: [("ok", GOOD)]})
    assert _fn()("x") == GOOD
    assert calls == [FABLE, FABLE]


def test_ordinary_error_result_failing_twice_raises_without_fallback(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", MAX_TURNS)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    with pytest.raises(RuntimeError, match="maximum number of turns"):
        _fn(cooldowns=cooldowns)("x")
    assert calls == [FABLE, FABLE]
    assert cooldowns == {}


def test_credits_error_on_the_same_model_retry_falls_back(monkeypatch):
    # flake first, then the account runs dry during the retry: still switches model
    calls = _install(monkeypatch, {FABLE: [("error", MAX_TURNS), ("error", CREDITS)],
                                   OPUS: [("ok", GOOD)]})
    assert _fn()("x") == GOOD
    assert calls == [FABLE, FABLE, OPUS]


def test_fallback_marker_error_skips_the_same_model_retry_when_fallback_is_enabled(monkeypatch):
    # 'credit balance' is retryable on main (it says 'error result'); with a chain
    # configured the retry would only repeat the error, so the next model is tried instead
    calls = _install(monkeypatch, {FABLE: [("error", "Your credit balance is too low")],
                                   OPUS: [("ok", GOOD)]})
    assert _fn()("x") == GOOD
    assert calls == [FABLE, OPUS]


def test_empty_fallbacks_disable_fallback(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)], OPUS: [("ok", GOOD)]})
    log = _Log()
    with pytest.raises(RuntimeError, match="Usage credits"):
        _fn(fallbacks=(), logger=log)("x")
    assert calls == [FABLE]
    assert log.fallbacks() == []


def test_empty_model_never_enables_fallback(monkeypatch):
    calls = _install(monkeypatch, {"": [("error", CREDITS)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    with pytest.raises(RuntimeError, match="Usage credits"):
        _fn(model="", cooldowns=cooldowns)("x")
    assert calls == [""]
    assert cooldowns == {}


@pytest.mark.parametrize("text", ["session_not_found", "file_not_found: /tmp/x",
                                  "not_found_error: resource missing"])
def test_non_model_not_found_errors_do_not_fall_back(monkeypatch, text):
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    with pytest.raises(RuntimeError, match="not_found"):
        _fn(cooldowns=cooldowns, logger=log)("x")
    assert OPUS not in calls and set(calls) == {FABLE}
    assert cooldowns == {} and log.fallbacks() == []


@pytest.mark.parametrize("text,marker", [
    ("not_found_error: model: claude-x", "not_found_error"),
    ("API Error: 404 model not available", "model not available"),
    ("model not found: claude-x", "model not found"),
    ("API Error: 400 Claude Code 2.1.165 does not support this model", "does not support this model"),
    ("There was an issue with the selected model. Try again or pick another.",
     "issue with the selected model"),
    ("usage limit reached", "usage limit"),
    ("You've hit your weekly limit for Opus \u00b7 resets Monday", "weekly limit"),
    ("You've reached your Opus limit for this week", "opus limit"),
    ("You've hit the limit for this model", "limit for this model"),
])
def test_model_specific_markers_fall_back(monkeypatch, text, marker):
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    log = _Log()
    cooldowns = {}
    assert _fn(logger=log, cooldowns=cooldowns, clock=lambda: 0.0)("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert log.fallbacks() == [{"from_model": FABLE, "to_model": OPUS, "reason": marker}]
    assert cooldowns == {FABLE: (3600.0, False)}  # only the failing model, not account-wide
    assert "sdk_query.account_cooldown" not in log.kinds()


def test_timeout_is_never_fallen_back(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [], OPUS: [("ok", GOOD)]}, stall=FABLE)
    cooldowns = {}
    with pytest.raises(TimeoutError, match="timed out"):
        _fn(timeout_s=0.05, cooldowns=cooldowns)("x")
    assert calls == [FABLE]
    assert cooldowns == {}


def test_other_exceptions_never_fall_back(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [ValueError("bad options")], OPUS: [("ok", GOOD)]})
    with pytest.raises(ValueError, match="bad options"):
        _fn()("x")
    assert calls == [FABLE]


# --- rev2: account-wide limits fail fast (cold audit of N7 rev5) ----------------------


def test_session_limit_cools_the_whole_chain_after_one_call_and_raises_unchanged(monkeypatch):
    # live 2026-10-15: 'session limit' is account-wide, so falling back made two more
    # failing calls per query. Now: one SDK call, every model cooling, error unchanged.
    calls = _install(monkeypatch, {FABLE: [("error", SESSION)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: 0.0, logger=log)
    with pytest.raises(RuntimeError, match="session limit") as exc:
        fn("x")
    assert str(exc.value) == f"claude-agent-sdk error result: {SESSION}"
    assert calls == [FABLE]
    assert cooldowns == {FABLE: (3600.0, True), OPUS: (3600.0, True), SONNET: (3600.0, True)}
    assert log.fallbacks() == []
    assert log.kinds() == ["sdk_query.failed", "sdk_query.account_cooldown"]
    cooldown = log.events[1][1]
    assert cooldown["model"] == FABLE and cooldown["reason"] == "session limit"
    assert cooldown["models"] == [FABLE, OPUS, SONNET]
    assert SESSION not in str(cooldown)  # marker only, never the error text


def test_session_limit_then_fail_fast_then_fable_again_after_cooldown(monkeypatch):
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", SESSION), ("ok", GOOD)],
                                   OPUS: [("ok", GOOD)], SONNET: [("ok", GOOD)]})
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"], logger=log)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    now["t"] = 100.0  # within the cooldown: zero SDK calls, the all-cooling error
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE]
    assert len(log.events) == 2  # nothing new was logged for the fast failure
    now["t"] = 3601.0  # expired: fable is tried again
    assert fn("x") == GOOD
    assert calls == [FABLE, FABLE]


def test_session_limit_on_the_last_model_still_raises_it_unchanged(monkeypatch):
    # model-specific errors earlier in the chain fall back as before; the account-wide
    # error at the end cools the chain and surfaces as the last error
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)], OPUS: [("error", CREDITS)],
                                   SONNET: [("error", SESSION)]})
    cooldowns = {}
    log = _Log()
    with pytest.raises(RuntimeError, match="session limit"):
        _fn(cooldowns=cooldowns, clock=lambda: 0.0, logger=log)("x")
    assert calls == [FABLE, OPUS, SONNET]
    assert cooldowns == {FABLE: (3600.0, True), OPUS: (3600.0, True), SONNET: (3600.0, True)}
    assert log.kinds() == ["sdk_query.failed", "sdk_query.model_fallback", "sdk_query.failed",
                           "sdk_query.model_fallback", "sdk_query.failed",
                           "sdk_query.account_cooldown"]


def test_empty_fallbacks_with_session_limit_keep_mains_behaviour(monkeypatch):
    # fallbacks=(): one non-retryable call, no cooldown, no extra event (byte-identical)
    calls = _install(monkeypatch, {FABLE: [("error", SESSION)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    with pytest.raises(RuntimeError, match="session limit") as exc:
        _fn(fallbacks=(), cooldowns=cooldowns, logger=log)("x")
    assert str(exc.value) == f"claude-agent-sdk error result: {SESSION}"
    assert calls == [FABLE]
    assert cooldowns == {}
    assert log.kinds() == ["sdk_query.failed"]


def test_planner_session_limit_cools_only_its_own_chain(monkeypatch):
    # a Sonnet planner's chain is [SONNET]: its account-wide cooldown must not reach into
    # the builder's chain for models it never had (fable is still tried by the builder)
    calls = _install(monkeypatch, {SONNET: [("error", SESSION)], FABLE: [("ok", GOOD)]})
    cooldowns = {}
    planner = _fn(model=SONNET, cooldowns=cooldowns, clock=lambda: 0.0)
    builder = _fn(cooldowns=cooldowns, clock=lambda: 1.0)
    with pytest.raises(RuntimeError, match="session limit"):
        planner("x")
    assert cooldowns == {SONNET: (3600.0, True)}
    assert builder("x") == GOOD
    assert calls == [SONNET, FABLE]


def test_planner_model_specific_failure_only_cools_that_model(monkeypatch):
    # model-specific cooldowns stay per model: the planner losing sonnet removes only
    # sonnet from a builder sharing the dict, never fable or opus
    calls = _install(monkeypatch, {SONNET: [("error", CREDITS)], FABLE: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    planner = _fn(model=SONNET, cooldowns=cooldowns, clock=lambda: 0.0, logger=log)
    builder = _fn(cooldowns=cooldowns, clock=lambda: 1.0)
    with pytest.raises(RuntimeError, match="Usage credits"):
        planner("x")
    assert cooldowns == {SONNET: (3600.0, False)}
    assert "sdk_query.account_cooldown" not in log.kinds()
    assert builder("x") == GOOD
    assert calls == [SONNET, FABLE]


def test_all_cooling_error_names_the_earliest_expiry(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)]})
    cooldowns = {FABLE: (500.0, False), OPUS: (50.0, False), SONNET: (900.0, True)}
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 10.0)("x")
    assert calls == []
    iso = str(exc.value).rsplit("until ", 1)[1]
    from datetime import datetime
    import time as _time
    before = _time.time()
    expiry = datetime.fromisoformat(iso).timestamp()
    # the earliest expiry is opus, 40s of clock time away from now
    assert before + 40 - 5 <= expiry <= before + 40 + 5
