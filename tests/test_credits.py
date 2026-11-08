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
              "CREDITS_START_BALANCE_USD", "CREDITS_EXPIRES",
              "CREDITS_CALL_RESERVE_USD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    credits.set_notifier(None)
    msgs: list[str] = []
    credits.set_notifier(msgs.append)
    st = tmp_path / "credits.json"
    st.write_text(json.dumps({"verified": True, "day": "2026-11-08",
                              "est_balance": 83.58}))
    yield SimpleNamespace(envf=envf, state=st, msgs=msgs, tmp=tmp_path)
    credits.set_notifier(None)


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
    # cap 15, reserve 2 -> 7 fit
    res = []
    lock = threading.Lock()

    def w():
        r = credits.reserve(NOW)
        with lock:
            res.append(r)
    ts = [threading.Thread(target=w) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for r in res if r) == 7


def test_cap_edge(env):
    put(env, spent_today=13.0)
    assert credits.reserve(NOW) is not None       # 13 + 2 <= 15
    put(env, spent_today=13.01, reserved=[])
    assert credits.reserve(NOW) is None
    assert credits.last_denial() == "daily cap"


def test_floor_edge(env):
    put(env, est_balance=7.0)
    assert credits.reserve(NOW) is not None       # 7 - 2 >= 5
    put(env, est_balance=6.99, reserved=[])
    assert credits.reserve(NOW) is None
    assert credits.last_denial() == "floor"


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
    put(env, reserved=[["old", 14.0, NOW - 31 * 60]])
    assert credits.reserve(NOW) is not None
    put(env, reserved=[["new", 14.0, NOW - 60]])
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
    assert credits.state(now=NOW)["spent_today"] > 0
    r = credits.reserve(NOW)
    before = credits.state(now=NOW)["spent_today"]
    credits.record(None, r, usage={"input_tokens": 1_000_000, "output_tokens": 0}, now=NOW)
    assert credits.state(now=NOW)["spent_today"] - before >= 15.0


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
    put(env, spent_today=14.0)
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
    assert "71.2" in out and KEY not in out
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
