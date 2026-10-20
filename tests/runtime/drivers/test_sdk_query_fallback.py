"""Model fallback chain in make_sdk_query_fn (operator decision 2026-10-15).

Fake claude_agent_sdk, no network, no sleep: the fake answers per model named in the
options, so every test states exactly which models were called and in what order.
"""
import asyncio
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from navigator.runtime.drivers.sdk_query import (_ACCOUNT_KEY, _PROBE_KEY, make_sdk_query_fn,
                                                 model_chain)
from navigator.safety.errors import QuotaExhaustedError

FABLE, OPUS, SONNET = "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"
CHAIN = (OPUS, SONNET)
GOOD = '{"commands": [], "summary": "ok"}'
CREDITS = "Usage credits are required for this model."
MAX_TURNS = "Reached maximum number of turns (4)"
SESSION = "You've hit your session limit"
SESSION_RESET = "You've hit your session limit \u00b7 resets 4:20am (America/New_York)"
ALL_COOLING = "all models cooling down until"
ET = ZoneInfo("America/New_York")
# 01:00 on an October (EDT) night in New York, on the injected wall clock
ONE_AM_ET = datetime(2026, 10, 15, 1, 0, tzinfo=ET).timestamp()
WALL = 1_000_000.0  # default injected wall clock: no test reads time.time()


def _local_delay(wall_now, hour, minute):
    """Clamped seconds from `wall_now` to the next local-time `hour:minute`, converted
    exactly as the driver does when the error names no timezone."""
    now_dt = datetime.fromtimestamp(wall_now).astimezone()
    target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    return float(min(max(target.timestamp() - wall_now, 60.0), 21600.0))


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
    kw.setdefault("wall_clock", lambda: WALL)
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
    # only the failing model, not account-wide; 'issue with the selected model' says
    # "try again", so it cools for 5 minutes instead of cooldown_s
    expected = 300.0 if marker == "issue with the selected model" else 3600.0
    assert cooldowns == {FABLE: (expected, False)}
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
    assert cooldowns == {_ACCOUNT_KEY: 3600.0}  # N7e: one entry for the whole account
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
    # N7e F1: the account-wide expiry no longer overwrites the credit cooldowns
    assert cooldowns == {FABLE: (3600.0, False), OPUS: (3600.0, False), _ACCOUNT_KEY: 3600.0}
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


def test_planner_session_limit_blocks_the_builder_without_a_call(monkeypatch):
    # N7c (audit F1): a session limit is account-wide, so the Sonnet planner's entry
    # blocks every model of the builder's chain [fable, opus, sonnet] with zero SDK calls
    calls = _install(monkeypatch, {SONNET: [("error", SESSION)], FABLE: [("ok", GOOD)]})
    cooldowns = {}
    planner = _fn(model=SONNET, cooldowns=cooldowns, clock=lambda: 0.0)
    builder = _fn(cooldowns=cooldowns, clock=lambda: 1.0, wall_clock=lambda: WALL)
    with pytest.raises(RuntimeError, match="session limit"):
        planner("x")
    assert cooldowns == {_ACCOUNT_KEY: 3600.0}  # a single-model chain blocks everyone too
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        builder("x")
    assert calls == [SONNET]
    assert cooldowns == {_ACCOUNT_KEY: 3600.0, _PROBE_KEY: 1.0}
    expected = datetime.fromtimestamp(WALL + 3599.0, tz=timezone.utc).isoformat(timespec="seconds")
    assert str(exc.value).endswith(f"until {expected}")


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
    cooldowns = {FABLE: (500.0, False), OPUS: (50.0, False), SONNET: (900.0, False)}
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 10.0, wall_clock=lambda: WALL)("x")
    assert calls == []
    # the earliest expiry is opus, 40s of clock time away: WALL + 40 on the injected wall
    # clock, never the real time.time()
    expected = datetime.fromtimestamp(WALL + 40.0, tz=timezone.utc).isoformat(timespec="seconds")
    assert expected == "1970-01-12T13:47:20+00:00"
    assert str(exc.value) == f"claude-agent-sdk: all models cooling down until {expected}"


def test_account_wide_entry_extends_the_all_cooling_expiry(monkeypatch):
    # an account-wide entry blocks every model until IT expires, however early the
    # model-specific entries end
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)]})
    cooldowns = {FABLE: (500.0, False), OPUS: (50.0, False), SONNET: (900.0, True)}
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 10.0, wall_clock=lambda: WALL)("x")
    assert calls == []
    expected = datetime.fromtimestamp(WALL + 890.0, tz=timezone.utc).isoformat(timespec="seconds")
    assert str(exc.value).endswith(f"until {expected}")


# --- N7c: reset times, all-cooling probes, selected-model cooldown --------------------


def test_reset_time_in_the_error_ends_the_cooldown_at_that_wall_time(monkeypatch):
    # 'resets 4:20am (America/New_York)' seen at 01:00 ET: the cooldown ends at 04:20 ET,
    # 3h20m away, not after the fixed cooldown_s
    calls = _install(monkeypatch, {FABLE: [("error", SESSION_RESET)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: 0.0,
             wall_clock=lambda: ONE_AM_ET, logger=log)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 12000.0}
    assert log.events[1][1]["cooldown_s"] == 12000.0


def test_reset_time_on_a_model_specific_error_cools_only_that_model(monkeypatch):
    text = "You've hit your usage limit \u00b7 resets 4:20am (America/New_York)"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET)
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (12000.0, False)}


def test_reset_more_than_six_hours_away_is_clamped(monkeypatch):
    text = "You've hit your session limit \u00b7 resets 10:30pm (America/New_York)"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns[_ACCOUNT_KEY] == 21600.0  # 21.5h away: clamped to 6h


def test_reset_less_than_a_minute_away_is_clamped_to_sixty_seconds(monkeypatch):
    text = "You've hit your session limit \u00b7 resets 1:00am (America/New_York)"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET - 30.0)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns[_ACCOUNT_KEY] == 60.0


def test_reset_without_a_timezone_uses_local_time_within_the_clamp(monkeypatch):
    # no '(tz)': the wall time is read in the machine's local zone; the expected delay is
    # computed with the same local conversion, so the assertion is exact on any machine
    text = "You've hit your session limit \u00b7 resets 10:30pm"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    expected = _local_delay(ONE_AM_ET, 22, 30)
    assert 60.0 <= expected <= 21600.0
    assert cooldowns[_ACCOUNT_KEY] == expected


def test_unknown_timezone_name_falls_back_to_local_time_without_raising(monkeypatch):
    text = "You've hit your session limit \u00b7 resets 10:30pm (Mars/Olympus)"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns[_ACCOUNT_KEY] == _local_delay(ONE_AM_ET, 22, 30)


@pytest.mark.parametrize("wall_dt,reset,expected", [
    # 12am is midnight (30 min from 23:30), not noon; 12pm is noon (3h from 09:00)
    (datetime(2026, 10, 15, 23, 30, tzinfo=ET), "resets 12am (America/New_York)", 1800.0),
    (datetime(2026, 10, 15, 11, 59, tzinfo=ET), "resets 12am (America/New_York)", 21600.0),
    (datetime(2026, 10, 15, 9, 0, tzinfo=ET), "resets 12pm (America/New_York)", 10800.0),
    (datetime(2026, 10, 15, 13, 0, tzinfo=ET), "resets 4pm (America/New_York)", 10800.0),
])
def test_hour_only_reset_times_are_read_exactly(monkeypatch, wall_dt, reset, expected):
    text = f"You've hit your session limit \u00b7 {reset}"
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: wall_dt.timestamp())
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns[_ACCOUNT_KEY] == expected


@pytest.mark.parametrize("text", ["You've hit your session limit \u00b7 resets soon",
                                  "You've hit your session limit \u00b7 resets Monday",
                                  "You've hit your session limit \u00b7 resets 25:99pm",
                                  SESSION])
def test_unparseable_or_absent_reset_uses_cooldown_s(monkeypatch, text):
    calls = _install(monkeypatch, {FABLE: [("error", text)], OPUS: [("ok", GOOD)]})
    cooldowns = {}
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: 0.0, wall_clock=lambda: ONE_AM_ET)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 3600.0}


def test_all_cooling_probes_the_first_model_once_after_five_minutes_and_clears_on_success(monkeypatch):
    # audit F2: with one run per proposal, failing every call for the whole cooldown lost
    # proposals even after the limit reset; now one probe per 5 minutes checks
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", SESSION), ("ok", GOOD)],
                                   OPUS: [("ok", GOOD)], SONNET: [("ok", GOOD)]})
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    now["t"] = 100.0  # first all-cooling encounter: zero calls, starts the probe interval
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    now["t"] = 399.0  # not yet 5 minutes: still zero calls
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE]
    now["t"] = 400.0  # 5 minutes on: exactly one probe on the first model of the chain
    assert fn("x") == GOOD
    assert calls == [FABLE, FABLE]
    assert cooldowns == {}  # success cleared every account-wide entry
    now["t"] = 401.0
    assert fn("x") == GOOD  # back to normal: fable first, no probe bookkeeping left
    assert calls == [FABLE, FABLE, FABLE]


def test_failed_probe_refreshes_the_cooldowns_and_raises_all_cooling(monkeypatch):
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", SESSION)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"], logger=log)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    now["t"] = 100.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    now["t"] = 400.0  # the probe fails with the same marker: all-cooling error, not the raw one
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE, FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 4000.0, _PROBE_KEY: 400.0}  # refreshed, gate kept
    assert log.kinds() == ["sdk_query.failed", "sdk_query.account_cooldown",
                           "sdk_query.failed", "sdk_query.account_cooldown"]
    now["t"] = 699.0  # within the next probe interval: zero calls again
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE, FABLE]


def test_builder_probe_clears_the_planners_account_wide_entry(monkeypatch):
    # the probe interval is per shared dict: the builder's successful probe lifts the
    # block the planner recorded
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {SONNET: [("error", SESSION)], FABLE: [("ok", GOOD)]})
    planner = _fn(model=SONNET, cooldowns=cooldowns, clock=lambda: now["t"])
    builder = _fn(cooldowns=cooldowns, clock=lambda: now["t"])
    with pytest.raises(RuntimeError, match="session limit"):
        planner("x")
    now["t"] = 1.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        builder("x")
    now["t"] = 301.0
    assert builder("x") == GOOD
    assert calls == [SONNET, FABLE]
    assert cooldowns == {}


def test_selected_model_error_cools_that_model_for_five_minutes(monkeypatch):
    # audit F4: Claude Code's 'issue with the selected model' says "try again"
    text = "There was an issue with the selected model. Try again or pick another."
    calls = _install(monkeypatch, {FABLE: [("error", text), ("ok", GOOD)], OPUS: [("ok", GOOD)]})
    now = {"t": 0.0}
    cooldowns = {}
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (300.0, False)}
    now["t"] = 301.0  # five minutes later fable is tried again
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, FABLE]


# --- N7d: probe target, no cooldown extension, gate reset only on success -----------


def test_probe_skips_a_model_in_its_own_cooldown_and_success_keeps_it_cooling(monkeypatch):
    # live scenario (N7c audit S1): Fable needs separate credits. Fable cooling for
    # credits + the planner's session limit: the builder's probe must go to opus (blocked
    # only by the account-wide entry); its success clears that entry but fable keeps
    # cooling until its own expiry
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS), ("ok", GOOD)],
                                   OPUS: [("ok", GOOD)], SONNET: [("error", SESSION)]})
    builder = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    planner = _fn(model=SONNET, cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    assert builder("x") == GOOD
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (3600.0, False)}
    now["t"] = 10.0
    with pytest.raises(RuntimeError, match="session limit"):
        planner("x")
    assert cooldowns == {FABLE: (3600.0, False), _ACCOUNT_KEY: 3610.0}
    now["t"] = 11.0  # account-wide: the builder is blocked, the probe interval starts
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        builder("x")
    assert calls == [FABLE, OPUS, SONNET]
    now["t"] = 311.0  # the probe goes to opus, never to the fable still cooling for credits
    assert builder("x") == GOOD
    assert calls == [FABLE, OPUS, SONNET, OPUS]
    assert cooldowns == {FABLE: (3600.0, False)}  # account-wide cleared, fable untouched
    now["t"] = 312.0
    assert builder("x") == GOOD
    assert calls == [FABLE, OPUS, SONNET, OPUS, OPUS]
    now["t"] = 3601.0  # fable's own cooldown expired: tried again
    assert builder("x") == GOOD
    assert calls == [FABLE, OPUS, SONNET, OPUS, OPUS, FABLE]


def test_every_chain_model_in_its_own_cooldown_under_an_account_limit_never_probes(monkeypatch):
    # nothing in the chain is blocked only by the account-wide entry (written here by a
    # role on another model): a probe would fail for a model's own reason, so none is made
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    seeded = {FABLE: (1e9, False), OPUS: 1e9, SONNET: (1e9, False),
              "claude-haiku-5": (1e9, True), _PROBE_KEY: 0.0}
    cooldowns = dict(seeded)
    log = _Log()
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        _fn(cooldowns=cooldowns, clock=lambda: 1000.0, logger=log)("x")
    assert calls == [] and log.events == []
    assert cooldowns == seeded


def test_healthy_traffic_without_an_account_block_clears_the_probe_gate(monkeypatch):
    # N7e F2: the gate only matters while an account-wide block is live (then NO chain has
    # candidates); a call that finds candidates means the outage is over, so the gate goes
    # instead of lingering as a stale timestamp for the next outage
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {SONNET: [("error", CREDITS)], FABLE: [("ok", GOOD)]})
    planner = _fn(model=SONNET, cooldowns=cooldowns, clock=lambda: now["t"])
    builder = _fn(cooldowns=cooldowns, clock=lambda: now["t"])
    with pytest.raises(RuntimeError, match="Usage credits"):
        planner("x")
    now["t"] = 1.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        planner("x")
    assert cooldowns == {SONNET: (3600.0, False), _PROBE_KEY: 1.0}
    now["t"] = 2.0
    assert builder("x") == GOOD
    assert cooldowns == {SONNET: (3600.0, False)}  # no account block: the gate is dropped
    now["t"] = 302.0  # sonnet still in its own cooldown: the gate restarts, nothing probed
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        planner("x")
    assert calls == [SONNET, FABLE]
    assert cooldowns == {SONNET: (3600.0, False), _PROBE_KEY: 302.0}


def test_probe_failing_with_credits_cools_only_the_probed_model(monkeypatch):
    # the probe on opus failing for credits cools opus; fable's unexpired cooldown and the
    # planner's account-wide entry are left exactly as they were, no fallback event
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)],
                                   OPUS: [("ok", GOOD), ("error", CREDITS)],
                                   SONNET: [("error", SESSION)]})
    log = _Log()
    builder = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"], logger=log)
    planner = _fn(model=SONNET, cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"],
                  logger=log)
    assert builder("x") == GOOD
    now["t"] = 10.0
    with pytest.raises(RuntimeError, match="session limit"):
        planner("x")
    now["t"] = 11.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        builder("x")
    now["t"] = 311.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        builder("x")
    assert calls == [FABLE, OPUS, SONNET, OPUS]
    assert cooldowns == {FABLE: (3600.0, False), _ACCOUNT_KEY: 3610.0,
                         OPUS: (3911.0, False), _PROBE_KEY: 311.0}
    assert log.kinds() == ["sdk_query.failed", "sdk_query.model_fallback", "sdk_query.completed",
                           "sdk_query.failed", "sdk_query.account_cooldown", "sdk_query.failed"]
    now["t"] = 610.0  # within the next interval: zero calls
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        builder("x")
    assert calls == [FABLE, OPUS, SONNET, OPUS]


# --- N7e: account-wide cooldown kept apart from model-specific ones; gate re-armed -----


def test_same_chain_session_limit_keeps_fables_own_credit_cooldown(monkeypatch):
    # N7e F1: fable out of credits, then opus hits the SESSION limit in the same call. The
    # account-wide entries used to overwrite fable's credit cooldown, so the probe went to
    # fable and fable was a candidate again as soon as the account limit lapsed
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS), ("ok", GOOD)],
                                   OPUS: [("error", SESSION), ("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"], logger=log)
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (3600.0, False), _ACCOUNT_KEY: 3600.0}
    assert log.kinds() == ["sdk_query.failed", "sdk_query.model_fallback", "sdk_query.failed",
                           "sdk_query.account_cooldown"]
    now["t"] = 1.0  # account-wide: zero calls, the probe interval starts
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE, OPUS]
    assert cooldowns == {FABLE: (3600.0, False), _ACCOUNT_KEY: 3600.0, _PROBE_KEY: 1.0}
    now["t"] = 301.0  # the probe targets opus (blocked only by the account), never fable
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, OPUS]
    assert cooldowns == {FABLE: (3600.0, False)}  # account block cleared, fable untouched
    now["t"] = 302.0  # fable is still no candidate: opus first
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, OPUS, OPUS]
    now["t"] = 3601.0  # fable's own credit cooldown expired: tried again
    assert fn("x") == GOOD
    assert calls == [FABLE, OPUS, OPUS, OPUS, FABLE]
    # the expired entry is inert and stays until fable fails again (as on main)
    assert cooldowns == {FABLE: (3600.0, False)}


def test_outage_ending_by_expiry_rearms_the_probe_gate_for_the_next_outage(monkeypatch):
    # N7e F2: the gate used to be cleared only by a successful probe, so after an outage
    # that ended by natural expiry the stale timestamp made the first all-cooling call of
    # the NEXT outage probe immediately
    now = {"t": 0.0}
    cooldowns = {}
    calls = _install(monkeypatch, {FABLE: [("error", SESSION), ("ok", GOOD),
                                           ("error", SESSION), ("ok", GOOD)],
                                   OPUS: [("ok", GOOD)], SONNET: [("ok", GOOD)]})
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"])
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    now["t"] = 100.0
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert cooldowns == {_ACCOUNT_KEY: 3600.0, _PROBE_KEY: 100.0}
    now["t"] = 3601.0  # the limit lapsed by itself: normal traffic drops the stale gate
    assert fn("x") == GOOD
    assert calls == [FABLE, FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 3600.0}  # expired, inert; the gate is gone
    now["t"] = 4000.0  # a new session limit
    with pytest.raises(RuntimeError, match="session limit"):
        fn("x")
    assert calls == [FABLE, FABLE, FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 7600.0}
    now["t"] = 4001.0  # first all-cooling call of the new outage: zero SDK calls
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE, FABLE, FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 7600.0, _PROBE_KEY: 4001.0}
    now["t"] = 4300.0  # 299s on: still no probe
    with pytest.raises(RuntimeError, match=ALL_COOLING):
        fn("x")
    assert calls == [FABLE, FABLE, FABLE]
    now["t"] = 4301.0  # the interval elapsed: one probe, success clears everything
    assert fn("x") == GOOD
    assert calls == [FABLE, FABLE, FABLE, FABLE]
    assert cooldowns == {}


def test_legacy_account_wide_model_entries_still_block_everyone_and_clear_on_probe(monkeypatch):
    # a dict written by an older navigator holds the account limit as (expiry, True) on
    # the failing role's model: it blocks the builder's whole chain with zero calls and a
    # successful probe removes it
    now = {"t": 0.0}
    cooldowns = {"claude-haiku-5": (3600.0, True)}
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    fn = _fn(cooldowns=cooldowns, clock=lambda: now["t"])
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        fn("x")
    assert calls == []
    expected = datetime.fromtimestamp(WALL + 3600.0, tz=timezone.utc).isoformat(timespec="seconds")
    assert str(exc.value).endswith(f"until {expected}")
    assert cooldowns == {"claude-haiku-5": (3600.0, True), _PROBE_KEY: 0.0}
    now["t"] = 300.0
    assert fn("x") == GOOD
    assert calls == [FABLE]
    assert cooldowns == {}


# --- N16: account-wide limits raise QuotaExhaustedError (class + bounded resets_at) ---


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def test_session_limit_is_quota_exhausted_with_bounded_resets_at(monkeypatch):
    assert _iso(ONE_AM_ET + 12000) == "2026-10-15T08:20:00+00:00"
    assert _iso(WALL + 3600) == "1970-01-12T14:46:40+00:00"
    calls = _install(monkeypatch, {FABLE: [("error", SESSION_RESET)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    cooldowns = {}
    log = _Log()
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: 0.0,
             wall_clock=lambda: ONE_AM_ET, logger=log)
    with pytest.raises(QuotaExhaustedError) as exc:
        fn("x")
    assert isinstance(exc.value, RuntimeError)
    assert str(exc.value) == f"claude-agent-sdk error result: {SESSION_RESET}"
    assert exc.value.marker == "session limit"
    assert exc.value.resets_at == "2026-10-15T08:20:00+00:00"  # 4:20am EDT, as the cooldown
    assert calls == [FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 12000.0}
    assert log.kinds() == ["sdk_query.failed", "sdk_query.account_cooldown"]
    assert SESSION_RESET not in str(log.events[1][1])
    # no reset time in the text: resets_at is the cooldown the chain actually entered
    calls = _install(monkeypatch, {FABLE: [("error", SESSION)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    with pytest.raises(QuotaExhaustedError) as exc:
        _fn(cooldown_s=3600, cooldowns={}, clock=lambda: 0.0, wall_clock=lambda: WALL)("x")
    assert exc.value.resets_at == "1970-01-12T14:46:40+00:00"
    assert calls == [FABLE]


def test_all_cooling_caused_by_an_account_block_is_quota_exhausted(monkeypatch):
    assert _iso(WALL + 3590) == "1970-01-12T14:46:30+00:00"
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)]})
    cooldowns = {_ACCOUNT_KEY: 3600.0}
    with pytest.raises(QuotaExhaustedError) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 10.0, wall_clock=lambda: WALL)("x")
    assert calls == []  # zero SDK calls
    assert str(exc.value) == "claude-agent-sdk: all models cooling down until 1970-01-12T14:46:30+00:00"
    assert exc.value.resets_at == "1970-01-12T14:46:30+00:00"
    assert exc.value.marker == "all models cooling"
    # a legacy (expiry, True) model entry is an account-wide block too
    legacy = {FABLE: (500.0, False), OPUS: (50.0, False), SONNET: (900.0, True)}
    with pytest.raises(QuotaExhaustedError):
        _fn(cooldowns=legacy, clock=lambda: 10.0, wall_clock=lambda: WALL)("x")
    assert calls == []


def test_all_cooling_from_model_only_cooldowns_is_not_quota_exhausted(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("ok", GOOD)]})
    cooldowns = {FABLE: (500.0, False), OPUS: (50.0, False), SONNET: (900.0, False)}
    with pytest.raises(RuntimeError, match=ALL_COOLING) as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 10.0, wall_clock=lambda: WALL)("x")
    assert not isinstance(exc.value, QuotaExhaustedError)
    assert calls == []


def test_failed_probe_raises_quota_exhausted(monkeypatch):
    # the live _ACCOUNT_KEY leaves no candidates; 400 - 0 >= 300 so the probe fires on
    # FABLE; SESSION refreshes _ACCOUNT_KEY to 4000 and the all-cooling error is raised
    calls = _install(monkeypatch, {FABLE: [("error", SESSION)], OPUS: [("ok", GOOD)],
                                   SONNET: [("ok", GOOD)]})
    now = {"t": 400.0}
    cooldowns = {_ACCOUNT_KEY: 3600.0, _PROBE_KEY: 0.0}
    fn = _fn(cooldown_s=3600, cooldowns=cooldowns, clock=lambda: now["t"], logger=_Log())
    with pytest.raises(QuotaExhaustedError) as exc:
        fn("x")
    assert calls == [FABLE]
    assert cooldowns == {_ACCOUNT_KEY: 4000.0, _PROBE_KEY: 400.0}
    assert exc.value.resets_at == "1970-01-12T14:46:40+00:00"  # WALL + (4000 - 400)
    assert str(exc.value).startswith("claude-agent-sdk: all models cooling down until ")
    # the cause keeps the text; the class carries none in its attributes
    assert exc.value.__cause__ is not None and "session limit" in str(exc.value.__cause__)
    assert SESSION not in exc.value.resets_at


def test_credits_errors_are_never_quota_exhausted(monkeypatch):
    calls = _install(monkeypatch, {FABLE: [("error", CREDITS)], OPUS: [("error", CREDITS)],
                                   SONNET: [("error", CREDITS)]})
    cooldowns = {}
    with pytest.raises(RuntimeError, match="Usage credits") as exc:
        _fn(cooldowns=cooldowns, clock=lambda: 0.0)("x")
    assert not isinstance(exc.value, QuotaExhaustedError)
    assert calls == [FABLE, OPUS, SONNET]
    assert _ACCOUNT_KEY not in cooldowns
