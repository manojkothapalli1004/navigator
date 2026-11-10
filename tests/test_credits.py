"""Tests for navigator.credits (subscription -> API credits fallback policy)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from navigator import credits

KEY = "sk-ant-TEST-KEY"
NOW = dt.datetime(2026, 11, 8, 12, 0, 0).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text(f'FOO=1\nANTHROPIC_API_KEY="{KEY}"\n')
    monkeypatch.setenv("NAVIGATOR_CREDITS_STATE", str(tmp_path / "credits.json"))
    monkeypatch.setenv("NAVIGATOR_CREDITS_ENV_FILE", str(envf))
    for k in ("CREDITS_FALLBACK", "CREDITS_DAILY_CAP_USD", "CREDITS_FLOOR_USD",
              "CREDITS_START_BALANCE_USD", "CREDITS_EXPIRES", "CREDITS_CALL_MAX_USD",
              "CREDITS_CALL_MIN_USD", "CREDITS_PROBE", "CREDITS_CALL_RESERVE_USD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAVIGATOR_CREDITS_LOG", str(tmp_path / "events.jsonl"))
    # the live probe (one haiku turn on the subscription) is stubbed: by default it confirms the limit,
    # so every switch test keeps its meaning; probe tests override it
    probes: list[int] = []
    monkeypatch.setattr(credits, "_probe_runner",
                        lambda timeout_s=60: (probes.append(1), ("limited", "stub: limit"))[1], raising=False)
    credits.set_notifier(None)
    msgs: list[str] = []
    credits.set_notifier(msgs.append)
    st = tmp_path / "credits.json"
    st.write_text(json.dumps({"verified": True, "day": "2026-11-08",
                              "est_balance": 83.58}))
    yield SimpleNamespace(envf=envf, state=st, msgs=msgs, tmp=tmp_path, probes=probes,
                          events=tmp_path / "events.jsonl")
    credits.set_notifier(None)


def events(env):
    try:
        return [json.loads(l) for l in env.events.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def ev(status="rejected", typ="five_hour", resets=None):
    return SimpleNamespace(rate_limit_info=SimpleNamespace(
        status=status, rate_limit_type=typ, resets_at=resets))


# 1 classify
@pytest.mark.parametrize("text", [
    "You've hit your limit · resets 4:20am",
    "Claude AI usage limit reached|1731000000",
    "5-hour session limit reached",
])
def test_classify_text_positive(text):
    assert credits.classify(text, now=NOW) is not None


def test_classify_event_positive():
    assert credits.classify(None, event=ev(resets=NOW + 100)).reset_epoch == NOW + 100
    assert credits.classify("x", event=ev(typ=None)) is not None
    assert credits.classify("x", event=ev(typ="seven_day")) is not None


@pytest.mark.parametrize("text", [
    "credit balance is too low", "usage credits exhausted", "opus limit reached",
    "weekly limit reached", "limit for this model", "429 too many requests",
    "rate_limit", "request timed out", "No conversation found with session ID",
    "not logged in", "billing_error", None,
])
def test_classify_text_negative(text):
    assert credits.classify(text, now=NOW) is None
    assert credits.classify(text, assistant_error="rate_limit", now=NOW) is None


def test_classify_event_negative():
    for e in (ev(status="allowed"), ev(typ="seven_day_opus"),
              ev(typ="seven_day_sonnet"), ev(typ="overage")):
        assert credits.classify("nothing", event=e) is None
    # model-scoped event beats matching text
    assert credits.classify("session limit", event=ev(typ="seven_day_opus")) is None


def test_is_credits_exhausted():
    assert credits.is_credits_exhausted("Credit balance is too low")
    assert credits.is_credits_exhausted("billing_error")
    assert not credits.is_credits_exhausted("session limit")


# 2 reset parsing
def test_reset_parsing():
    assert credits.classify("session limit", event=ev(resets=NOW + 5)).reset_epoch == NOW + 5
    lim = credits.classify("You've hit your limit · resets 4:20am (America/New_York)", now=NOW)
    assert lim.reset_epoch and lim.reset_epoch > NOW
    assert lim.reset_epoch - NOW <= 86400 + 3600
    assert credits.classify("usage limit reached|1731000000", now=NOW).reset_epoch == 1731000000
    assert credits.classify("session limit", now=NOW).reset_epoch is None


def test_on_limit_clamp_and_default(env):
    r = credits.on_limit(credits.Limit(NOW + 5, "x"), NOW)
    assert r and credits.state(now=NOW)["until"] == NOW + 60
    credits.release(r.reservation)
    credits.on_limit(credits.Limit(NOW + 99 * 86400, "x"), NOW + 61)
    assert credits.state(now=NOW)["until"] == NOW + 61 + 7 * 86400
    env.state.unlink()
    env.state.write_text(json.dumps({"verified": True, "day": "2026-11-08"}))
    credits.on_limit(credits.Limit(None, "x"), NOW)
    assert credits.state(now=NOW)["until"] == NOW + 3600


# 3 state
def test_state_defaults_corrupt_and_rollover(env):
    env.state.write_text("{not json")
    s = credits.state(now=NOW)
    assert s["est_balance"] == 83.58 and s["spent_today"] == 0
    env.state.write_text(json.dumps({"day": "2026-11-07", "spent_today": 9.0,
                                     "est_balance": 50.0}))
    s = credits.state(now=NOW)
    assert s["spent_today"] == 0 and s["est_balance"] == 50.0 and s["day"] == "2026-11-08"


def test_state_file_mode_and_no_key(env):
    r = credits.on_limit(credits.Limit(NOW + 600, "x"), NOW)
    assert r
    assert stat.S_IMODE(os.stat(env.state).st_mode) == 0o600
    assert KEY not in env.state.read_text()
    assert not list(env.tmp.glob("*.tmp"))


def test_two_threads_no_lost_update(env):
    def work():
        for _ in range(10):
            r = credits.reserve(NOW)
            credits.record(0.01, r, now=NOW)
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    s = credits.state(now=NOW)
    assert abs(s["spent_today"] - 40 * 0.011) < 1e-6
    assert abs(s["est_balance"] - (83.58 - 40 * 0.011)) < 1e-6


# 4 guard / reserve
def put(env, **kw):
    s = json.loads(env.state.read_text())
    s.update(kw)
    env.state.write_text(json.dumps(s))


def test_reserve_admits_exactly_k(env):
    # cap 15, each call budgeted at most 5 -> exactly 3 fit and their budgets sum to the cap
    res = []
    lock = threading.Lock()

    def w():
        r = credits.reserve(NOW)
        with lock:
            res.append(r)
    ts = [threading.Thread(target=w) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    got = [r for r in res if r]
    assert len(got) == 3 and abs(sum(r.amount for r in got) - 15.0) < 1e-9
    assert credits.last_denial() == "busy"            # nothing spent yet: holds, not the cap, filled the room


def test_busy_is_not_a_cap_notice(env):
    for _ in range(3):
        assert credits.reserve(NOW) is not None
    put(env, until=NOW + 600)
    assert credits.route(NOW).denied == "busy"
    assert not [m for m in env.msgs if "paused" in m]  # a 4th concurrent call waits quietly
    assert [e for e in events(env) if e["kind"] == "denied"][-1]["reason"] == "busy"


def test_cap_edge(env):
    put(env, spent_today=14.5)
    r = credits.reserve(NOW)
    assert r is not None and abs(r.amount - 0.5) < 1e-9   # 0.5 of headroom is still worth a call
    put(env, spent_today=14.6, reserved=[])
    assert credits.reserve(NOW) is None                    # below CREDITS_CALL_MIN_USD
    assert credits.last_denial() == "daily cap"


def test_floor_edge(env):
    put(env, est_balance=5.5)
    r = credits.reserve(NOW)
    assert r is not None and abs(r.amount - 0.5) < 1e-9   # 5.5 - 5 floor = 0.5 headroom
    put(env, est_balance=5.4, reserved=[])
    assert credits.reserve(NOW) is None
    assert credits.last_denial() == "floor"


# budget-bounded calls (2026-11-09): the reservation IS the call's --max-budget-usd
def test_budget_is_min_of_call_max_daily_and_balance_headroom(env, monkeypatch):
    r = credits.reserve(NOW)
    assert r is not None and r.amount == 5.0                  # call max binds (cap 15, balance 83.58)
    credits.release(r)
    put(env, day="2026-11-08", spent_today=12.0, reserved=[])  # release() stamps the real day; pin NOW's
    assert credits.reserve(NOW).amount == 3.0                  # daily headroom binds
    put(env, day="2026-11-08", spent_today=0.0, est_balance=7.25, reserved=[])
    assert credits.reserve(NOW).amount == 2.25                 # balance headroom above the floor binds
    monkeypatch.setenv("CREDITS_CALL_MAX_USD", "1.5")
    put(env, day="2026-11-08", spent_today=0.0, est_balance=83.58, reserved=[])
    assert credits.reserve(NOW).amount == 1.5


def test_concurrent_reservations_share_the_headroom(env):
    a = credits.reserve(NOW)
    put(env, spent_today=8.0)                                  # 7 left; a holds 5 -> 2 for the next
    b = credits.reserve(NOW)
    assert a.amount == 5.0 and b.amount == 2.0
    assert credits.reserve(NOW) is None and credits.last_denial() == "busy"
    rt = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert rt is None                                          # nothing left to hand out
    credits.release(a)
    assert credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW + 1).budget_usd == round(5.0 / 1.1, 4)


def test_route_carries_budget(env):
    put(env, until=NOW + 600)
    rt = credits.route(NOW)
    assert rt.env and rt.reservation.amount == 5.0
    assert rt.budget_usd == round(5.0 / 1.1, 4)      # what the CLI may spend; x1.10 recorded == the hold
    assert credits.Route().budget_usd is None


def test_tests_never_touch_the_live_ledger():
    # the autouse conftest fixture must hold for every test, fixture or not (2026-11-09 incident)
    for p in (credits.state_path(), credits.env_file(), credits.events_path()):
        assert "/Users/Manoj/calwbot/logs" not in p and "orchestrator/.env" not in p, p
    assert credits._probe_runner()[0] == "limited"


def test_exhausted_denies(env):
    put(env, exhausted=True)
    assert credits.reserve(NOW) is None and credits.last_denial() == "credits exhausted"


def test_expiry_boundaries_and_env_only_shortens(env, monkeypatch):
    t = dt.datetime(2026, 11, 17, 23, 59).timestamp()
    put(env, day="2026-11-17")
    assert credits.reserve(t) is None and credits.last_denial() == "expired"
    t = dt.datetime(2026, 11, 18, 0, 0).timestamp()
    put(env, day="2026-11-18")
    assert credits.reserve(t) is None
    t = dt.datetime(2026, 11, 17, 10, 0).timestamp()
    put(env, day="2026-11-17")
    assert credits.reserve(t) is not None
    monkeypatch.setenv("CREDITS_EXPIRES", "2027-01-01")   # cannot extend
    assert credits.effective_expiry() == dt.date(2026, 11, 18)
    monkeypatch.setenv("CREDITS_EXPIRES", "garbage")
    assert credits.effective_expiry() == dt.date(2026, 11, 18)
    monkeypatch.setenv("CREDITS_EXPIRES", "2026-11-10")   # can shorten
    assert credits.effective_expiry() == dt.date(2026, 11, 10)
    assert credits.reserve(t) is None


def test_stale_reservation_dropped(env):
    put(env, reserved=[["old", 14.6, NOW - 51 * 60]])       # older than STALE_RESERVATION_S (50 min)
    assert credits.reserve(NOW) is not None
    put(env, reserved=[["new", 14.6, NOW - 45 * 60]])       # a 45-min turn is still live: hold kept
    assert credits.reserve(NOW) is None


def test_record_cost_margin_and_release(env):
    r = credits.reserve(NOW)
    credits.record(1.0, r, model="opus", now=NOW)
    s = credits.state(now=NOW)
    assert abs(s["spent_today"] - 1.1) < 1e-9 and not s.get("reserved")
    r = credits.reserve(NOW)
    assert credits.state(now=NOW)["reserved"]
    credits.release(r)
    assert not credits.state(now=NOW)["reserved"]


def test_missing_cost_never_free(env):
    r = credits.reserve(NOW)
    credits.record(None, r, now=NOW)
    assert abs(credits.state(now=NOW)["spent_today"] - r.amount) < 1e-9   # the hold: the most it could have spent
    r = credits.reserve(NOW)
    before = credits.state(now=NOW)["spent_today"]
    credits.record(None, r, usage={"input_tokens": 1_000_000, "output_tokens": 0}, now=NOW)
    assert credits.state(now=NOW)["spent_today"] - before >= 15.0            # unknown model = opus prices


def test_estimate_prices_by_model(env):
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000}
    sonnet = credits._estimate(usage, "claude-sonnet-5-5")
    opus = credits._estimate(usage, "claude-opus-5-5")
    haiku = credits._estimate(usage, "haiku")
    assert haiku < sonnet < opus
    assert abs(sonnet - (3.0 + 1.5 + 0.6)) < 1e-9 and abs(opus - (15 + 7.5 + 3.0)) < 1e-9
    assert credits._estimate(usage, "claude-fable-5-1") == opus and credits._estimate(usage, None) == opus
    assert credits._estimate(None, "sonnet") == 5.0 and credits._estimate({}, "sonnet", fallback=2.0) == 2.0


def test_record_at_cap_closes_window_once(env):
    put(env, until=NOW + 3600, spent_today=10.0)
    r = credits.reserve(NOW)
    credits.record(5.0, r, model="opus", now=NOW)            # 10 + 5.5 >= 15
    s = credits.state(now=NOW)
    assert s["until"] == 0 and s["spent_today"] >= 15.0
    stops = [m for m in env.msgs if "daily cap" in m and "reached" in m]
    assert len(stops) == 1
    assert [e["kind"] for e in events(env)][-2:] == ["spend", "cap_reached"]
    credits.record(0.1, None, model="opus", now=NOW + 1)     # already closed: no second notice
    assert len([m for m in env.msgs if "reached" in m]) == 1


# confirmed limits (2026-11-09): credits only when the subscription really is out
def test_probe_ok_means_no_switch(env, monkeypatch):
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("ok", "ok"))
    assert credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW) is None
    s = credits.state(now=NOW)
    assert s["until"] == 0 and s["switched_at"] == 0 and s["reserved"] == []
    assert not [m for m in env.msgs if "using API credits" in m]
    ev = [e for e in events(env) if e["kind"] == "probe"]
    assert ev and ev[-1]["result"] == "ok" and credits.last_denial() == "probe ok"
    # the operator hears once per day why nothing switched
    assert len([m for m in env.msgs if "still works" in m]) == 1
    credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW + 61)
    assert len([m for m in env.msgs if "still works" in m]) == 1


def test_probe_text_is_redacted_before_truncation(env, monkeypatch):
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("unknown", "x" * 190 + KEY))
    credits.on_limit(credits.Limit(None, "text"), NOW)
    text = env.events.read_text()
    assert KEY not in text and KEY[:12] not in text and "[redacted]" in text


def test_probe_unknown_fails_closed(env, monkeypatch):
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("unknown", "probe timed out"))
    assert credits.on_limit(credits.Limit(None, "text"), NOW) is None
    assert credits.state(now=NOW)["until"] == 0 and credits.last_denial() == "probe unknown"
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: (_ for _ in ()).throw(OSError("no cli")))
    assert credits.on_limit(credits.Limit(None, "text"), NOW + 61) is None


def test_probe_limited_switches_and_is_not_repeated_while_on(env):
    rt = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert rt is not None and rt.budget_usd == round(5.0 / 1.1, 4) and env.probes == [1]
    credits.release(rt.reservation)
    credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW + 1)   # window already open: no probe
    assert env.probes == [1]
    kinds = [e["kind"] for e in events(env)]
    assert kinds.count("probe") == 1 and kinds.count("switch") == 1


def test_probe_result_reused_within_a_minute(env, monkeypatch):
    calls = []
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: (calls.append(1), ("ok", "ok"))[1])
    assert credits.on_limit(credits.Limit(None, "text"), NOW) is None
    assert credits.on_limit(credits.Limit(None, "text"), NOW + 30) is None
    assert calls == [1]                                           # reused
    assert credits.on_limit(credits.Limit(None, "text"), NOW + 61) is None
    assert calls == [1, 1]                                        # probed again after 60 s
    assert any(e.get("reused") for e in events(env) if e["kind"] == "probe")


def test_probe_switch_off(env, monkeypatch):
    monkeypatch.setenv("CREDITS_PROBE", "0")
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("ok", "must not be called"))
    assert credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW) is not None


def test_open_window_closes_when_the_subscription_answers(env, monkeypatch):
    put(env, until=NOW + 3600, switched_at=NOW - 10)
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("ok", "ok"))
    rt = credits.route(NOW)
    assert rt.env == {} and rt.denied is None
    s = credits.state(now=NOW)
    assert s["until"] == 0 and s["reserved"] == []
    assert [e["kind"] for e in events(env)] == ["probe", "window_closed"]
    assert len([m for m in env.msgs if "answers again" in m]) == 1
    put(env, until=NOW + 3600)
    credits.route(NOW + 61)                                          # a second close that day: no second notice
    assert len([m for m in env.msgs if "answers again" in m]) == 1


def test_open_window_unknown_probe_stays_on_subscription_without_closing(env, monkeypatch):
    put(env, until=NOW + 3600)
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: ("unknown", "timeout"))
    assert credits.route(NOW).env == {}
    assert credits.state(now=NOW)["until"] == NOW + 3600            # left open for a later confirming probe


def test_open_window_probe_once_a_minute(env):
    put(env, until=NOW + 3600)
    for t in (NOW, NOW + 10, NOW + 59):
        rt = credits.route(t)
        assert rt.env
        credits.release(rt.reservation)
    assert env.probes == [1]
    credits.release(credits.route(NOW + 61).reservation)
    assert env.probes == [1, 1]


def test_probe_interpretation_order():
    I = credits._probe_interpret
    assert I({"is_error": False, "text": "ok", "limited": True})[0] == "ok"     # an answer wins
    assert I({"is_error": True, "text": "boom", "limited": True})[0] == "limited"
    assert I({"is_error": True, "text": "You've hit your limit"})[0] == "limited"
    assert I({"is_error": True, "text": "error_max_turns"})[0] == "unknown"
    assert I({"exc": "CLIConnectionError: no cli"})[0] == "unknown"
    assert I({"exc": "ResultError: session limit reached"})[0] == "limited"
    assert I({})[0] == "unknown"
    assert I({"is_error": False, "on_key": True})[0] == "unknown"
    out = {}
    credits._probe_observe(out, ev(typ="seven_day_opus"))               # model-scoped rejection: not a limit
    assert "limited" not in out
    credits._probe_observe(out, ev(typ="five_hour"))
    assert out["limited"] is True
    credits._probe_observe(out, SimpleNamespace(data={"apiKeySource": "ANTHROPIC_API_KEY"}))
    assert out["on_key"] is True


def test_on_limit_async_releases_a_hold_when_cancelled(env, monkeypatch):
    import asyncio
    import threading
    gate = threading.Event()

    def slow_probe(timeout_s=60):
        gate.wait(5)
        return "limited", "late"

    monkeypatch.setattr(credits, "_probe_runner", slow_probe)
    released = []
    real_release = credits.release
    monkeypatch.setattr(credits, "release", lambda r, *a, **k: (released.append(r), real_release(r, *a, **k)))

    async def go():
        task = asyncio.ensure_future(credits.on_limit_async(credits.Limit(NOW + 600, "five_hour")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(150):                     # the worker finishes, switches, and its Route is dropped
            await asyncio.sleep(0.02)
            if released:
                break
        return credits.state()
    s = asyncio.run(go())
    assert len(released) == 1 and released[0].amount > 0 and s["reserved"] == [] and s["until"] > 0


def test_route_async_runs_off_the_loop_and_returns_the_route(env, monkeypatch):
    import asyncio
    import threading
    idents = []
    real = credits.route
    monkeypatch.setattr(credits, "route", lambda *a, **k: (idents.append(threading.get_ident()), real(*a, **k))[1])
    put(env, until=time.time() + 600, day=credits._today(time.time()))   # route_async uses the real clock
    rt = asyncio.run(credits.route_async())
    assert rt.env and idents and idents[0] != threading.main_thread().ident
    credits.release(rt.reservation)


def test_open_window_probe_skipped_when_the_guard_denies(env):
    put(env, until=NOW + 600, spent_today=14.9)
    assert credits.route(NOW).denied == "daily cap"
    assert env.probes == []                                   # no point probing: the call could not run anyway


def test_pending_probe_keeps_concurrent_callers_on_the_subscription(env):
    put(env, until=NOW + 600, probe_result="pending", probe_at=NOW - 5)
    assert credits.route(NOW).env == {}                       # window open, another caller is probing: stay
    put(env, until=0, probe_result="pending", probe_at=NOW - 5)
    assert credits.on_limit(credits.Limit(None, "text"), NOW) is None   # a new limit while a probe is pending
    assert credits.last_denial() == "probe pending" and env.probes == []
    assert not [m for m in env.msgs if "probe" in m.lower() or "limit" in m.lower()]
    put(env, until=NOW + 600, probe_result="pending", probe_at=NOW - 61)   # a stale claim is ignored
    assert credits.route(NOW).env and env.probes == [1]


def test_probe_notices_once_per_day_per_result(env, monkeypatch):
    results = iter(["ok", "unknown", "ok", "unknown"])
    monkeypatch.setattr(credits, "_probe_runner", lambda timeout_s=60: (next(results), "t"))
    for i in range(4):
        credits.on_limit(credits.Limit(None, "text"), NOW + i * 61)
    assert len([m for m in env.msgs if "still works" in m]) == 1
    assert len([m for m in env.msgs if "could not confirm" in m]) == 1


def test_cli_events_clamps_n(env, capsys):
    credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert credits.main(["events", "0"]) == 0
    assert capsys.readouterr().out.count('"kind"') == 1


# events file
def test_events_written_and_key_free(env):
    rt = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    credits.record(0.5, rt.reservation, model="claude-sonnet-5-5",
                   usage={"input_tokens": 10, "output_tokens": 2}, now=NOW + 1)
    put(env, spent_today=14.9)
    assert credits.route(NOW + 2).denied == "daily cap"
    kinds = [e["kind"] for e in events(env)]
    assert kinds == ["probe", "switch", "spend", "denied"]   # guard denies first: no probe when nothing could run
    spend = [e for e in events(env) if e["kind"] == "spend"][0]
    assert spend["model"] == "claude-sonnet-5-5" and abs(spend["cost"] - 0.55) < 1e-9
    assert spend["tokens"] == {"input": 10, "output": 2, "cache_read": 0, "cache_creation": 0}
    assert spend["reservation"] == rt.reservation.id
    text = env.events.read_text()
    assert KEY not in text
    assert stat.S_IMODE(os.stat(env.events).st_mode) == 0o600


def test_events_never_raise(env, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_CREDITS_LOG", str(env.tmp / "no" / "dir" / "x" / "events.jsonl"))
    # an unwritable location (a file where a directory must be) must not break recording
    (env.tmp / "no").write_text("not a dir")
    r = credits.reserve(NOW)
    credits.record(0.1, r, now=NOW)
    assert credits.state(now=NOW)["spent_today"] > 0


def test_cli_events(env, capsys):
    credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert credits.main(["events", "1"]) == 0
    out = capsys.readouterr().out
    assert '"kind": "switch"' in out and "2 events" in out and KEY not in out
    assert credits.main(["events", "x"]) == 2


def test_events_trimmed_past_cap(env):
    """Housekeeping 2026-11-10: the events file is capped (navigator.jsonl_log, 2 MiB by default):
    past it only the newest lines stay (at most half the cap), the line being written is never
    the one lost, and the file stays 0600 and key-free."""
    old = json.dumps({"ts": "2026-11-01T00:00:00", "kind": "spend", "pad": "x" * 200}) + "\n"
    env.events.write_text(old * (2 * 1024 * 1024 // len(old) + 10))   # just over the default cap
    os.chmod(env.events, 0o600)
    assert env.events.stat().st_size > 2 * 1024 * 1024
    rt = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert rt
    assert env.events.stat().st_size <= 1024 * 1024
    evs = events(env)
    assert len(evs) <= 2000
    assert [e["kind"] for e in evs[-2:]] == ["probe", "switch"]
    assert KEY not in env.events.read_text()
    assert stat.S_IMODE(os.stat(env.events).st_mode) == 0o600
    assert not list(env.tmp.glob("*.tmp"))


# 5 key hygiene
def test_load_key_call_time(env):
    assert credits.load_key() == KEY
    env.envf.write_text("ANTHROPIC_API_KEY=sk-other\n")
    assert credits.load_key() == "sk-other"
    env.envf.write_text("X=1\n")
    assert credits.load_key() is None
    env.envf.unlink()
    assert credits.load_key() is None


def test_redact(env):
    assert credits.redact(f"a {KEY} b") == "a [redacted] b"
    assert credits.redact("plain") == "plain"
    env.envf.unlink()
    assert credits.redact("plain") == "plain"


def test_key_never_leaks(env, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    objs = []
    r = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    objs += [r, r.reservation, credits.state(now=NOW)]
    rt = credits.route(NOW + 1)
    objs.append(rt)
    credits.record(0.5, rt.reservation, model="m", now=NOW + 1)
    credits.mark_exhausted()
    objs.append(credits.route(NOW + 2))
    assert rt.env == {"ANTHROPIC_API_KEY": KEY}
    for o in objs:
        assert KEY not in repr(o) and KEY not in str(o)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err + caplog.text + "".join(env.msgs)
    assert KEY not in env.state.read_text()
    assert KEY not in env.events.read_text()


# 6 kill switch, verified gate, already-on-key
def test_kill_switch(env, monkeypatch):
    monkeypatch.setenv("CREDITS_FALLBACK", "0")
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None
    put(env, until=NOW + 600)
    assert credits.route(NOW).env == {}


def test_unverified_gate(env):
    put(env, verified=False)
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None
    put(env, until=NOW + 600)
    r = credits.route(NOW)
    assert r.env == {} and r.denied == "unverified"


def test_already_on_key_noop(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "whatever")
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None


def test_inherited_key_warns_once_per_process(env, monkeypatch, caplog):
    """Housekeeping 2026-11-10 option (c): an ANTHROPIC_API_KEY already in this process's environment
    runs every call on the key with no budget, cap or ledger. It is announced ONCE per process (a
    warning, an inherited_key event, a notice), even with CREDITS_FALLBACK=0, and never with the
    key value; route()/on_limit() return exactly what they did before."""
    inherited = "sk-ant-INHERITED-VALUE"
    monkeypatch.setattr(credits, "_inherited_warned", False, raising=False)   # an earlier test consumed it
    monkeypatch.setenv("ANTHROPIC_API_KEY", inherited)
    caplog.set_level(logging.INFO, logger="navigator.credits")
    put(env, until=NOW + 600)                          # a window that would otherwise route to credits
    for i in range(2):
        assert credits.route(NOW + i).env == {}
        assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW + i) is None

    def inherited_events():
        return [e for e in events(env) if e["kind"] == "inherited_key"]

    def notices():
        return [m for m in env.msgs if "ANTHROPIC_API_KEY is in this process's environment" in m]

    assert len(inherited_events()) == 1
    e = inherited_events()[0]
    assert e["pid"] == os.getpid() and isinstance(e["argv0"], str) and "/" not in e["argv0"]
    assert len(notices()) == 1 and "--revert" in notices()[0]
    assert env.msgs == notices()
    warns = [r for r in caplog.records if r.getMessage() == "credits.inherited_key"]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING
    # the kill switch does not hide it: the announcement runs before the enabled() check
    monkeypatch.setattr(credits, "_inherited_warned", False)
    monkeypatch.setenv("CREDITS_FALLBACK", "0")
    assert credits.route(NOW).env == {}
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None
    assert len(inherited_events()) == 2 and len(notices()) == 2
    # threads racing the first call announce it once: the flag is set before anything is emitted
    monkeypatch.setattr(credits, "_inherited_warned", False)
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        credits.route(NOW)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(inherited_events()) == 3 and len(notices()) == 3
    assert inherited not in env.events.read_text() + "".join(env.msgs) + caplog.text


def test_no_key_denies(env):
    env.envf.write_text("X=1\n")
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None


# route behaviour and notices
def test_route_window_and_notice_once(env):
    assert credits.route(NOW).env == {}
    r = credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW)
    assert r.env["ANTHROPIC_API_KEY"] == KEY
    credits.release(r.reservation)
    credits.on_limit(credits.Limit(NOW + 600, "five_hour"), NOW + 1)  # still on
    assert len([m for m in env.msgs if "using API credits" in m]) == 1
    assert credits.route(NOW + 10).env
    assert credits.route(NOW + 601).env == {}          # window passed -> probe


def test_guard_stop_notice_once(env):
    put(env, spent_today=14.6)
    assert credits.on_limit(credits.Limit(NOW + 600, "x"), NOW) is None
    assert credits.route(NOW + 1).denied == "daily cap"
    stops = [m for m in env.msgs if "paused" in m]
    assert len(stops) == 1 and "daily cap" in stops[0]


def test_mark_exhausted(env):
    put(env, until=NOW + 600)
    credits.mark_exhausted()
    s = credits.state(now=NOW)
    assert s["exhausted"] and s["until"] == 0
    assert any("exhausted" in m for m in env.msgs)


# CLI
def test_cli_status_set_balance(env, capsys):
    assert credits.main(["set-balance", "71.20"]) == 0
    assert credits.state(now=NOW)["est_balance"] == 71.20
    assert credits.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "71.2" in out and KEY not in out and "held=" in out and "probe=" in out
    put(env, exhausted=True)
    assert credits.main(["reset-exhausted"]) == 0
    assert not credits.state(now=NOW)["exhausted"]


def test_cli_verify_stubbed(env, capsys):
    put(env, verified=False)
    calls = []

    def ok(key):
        calls.append(1)
        return {"api_key_source": "ANTHROPIC_API_KEY", "total_cost_usd": 0.001}
    assert credits.main(["verify"], query=ok) == 0
    assert credits.state(now=NOW)["verified"] is True and calls
    put(env, verified=False)

    def login(key):
        return {"api_key_source": "none", "total_cost_usd": 0.0}
    assert credits.main(["verify"], query=login) == 1
    assert credits.state(now=NOW)["verified"] is False
    assert KEY not in capsys.readouterr().out


def test_parse_reset_invalid_time_returns_none():
    assert credits._parse_reset("limit hit, resets 25:99pm", 1_700_000_000.0) is None
