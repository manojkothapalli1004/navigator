"""Subscription -> API-credits fallback policy (single owner; sync, stdlib only).

The key is read from the .env file at call time, never cached, never logged,
never stored in state, and only ever handed out inside ``Route.env``
(hidden from repr). ``total_cost_usd`` is an estimate, spend is bounded by the
pre-call reservation, the daily cap, the floor and the expiry; one in-flight
call can still overshoot by what a single capped call costs (hence RESERVE).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

log = logging.getLogger("navigator.credits")

HARD_EXPIRY = dt.date(2026, 11, 18)
MARGIN = 1.10
DEFAULT_WINDOW_S = 3600
MAX_WINDOW_S = 7 * 86400
STALE_RESERVATION_S = 30 * 60
KEY_NAME = "ANTHROPIC_API_KEY"

# conservative (opus) $/Mtok
_PRICE = {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_creation": 18.75}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def state_path() -> str:
    return os.environ.get("NAVIGATOR_CREDITS_STATE") or "/Users/Manoj/calwbot/logs/navigator/credits.json"


def env_file() -> str:
    return os.environ.get("NAVIGATOR_CREDITS_ENV_FILE") or "/Users/Manoj/calwbot/orchestrator/.env"


def enabled() -> bool:
    return os.environ.get("CREDITS_FALLBACK", "1") != "0"


def daily_cap() -> float:
    return _f("CREDITS_DAILY_CAP_USD", 15)


def floor() -> float:
    return _f("CREDITS_FLOOR_USD", 5)


def reserve_amount() -> float:
    return _f("CREDITS_CALL_RESERVE_USD", 2.0)


def start_balance() -> float:
    return _f("CREDITS_START_BALANCE_USD", 83.58)


def effective_expiry() -> dt.date:
    raw = os.environ.get("CREDITS_EXPIRES")
    if raw:
        try:
            return min(dt.date.fromisoformat(raw.strip()), HARD_EXPIRY)
        except ValueError:
            pass
    return HARD_EXPIRY


# ---------------------------------------------------------------- detection

@dataclass(frozen=True)
class Limit:
    reset_epoch: float | None
    kind: str = "subscription"


_NEVER = (
    "credit balance is too low", "usage credits", "billing_error",
    "opus limit", "weekly limit", "limit for this model",
    "issue with the selected model", "model not found",
    "authentication_failed", "invalid_request", "server_error",
    "not logged in", "no conversation found", "overloaded",
)
_ACCOUNT_WIDE = {"five_hour", "seven_day", None}
_EXHAUSTED = ("credit balance is too low", "billing_error")
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?", re.I)
_PIPE_RE = re.compile(r"limit reached\|(\d{9,11})", re.I)


def is_credits_exhausted(text: str | None) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _EXHAUSTED)


def _parse_reset(text: str, now: float) -> float | None:
    m = _PIPE_RE.search(text)
    if m:
        return float(m.group(1))
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    minute = int(m.group(2) or 0)
    try:
        tz = ZoneInfo(m.group(4).strip()) if m.group(4) else None
    except Exception:
        tz = None
    base = dt.datetime.fromtimestamp(now, tz)
    cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += dt.timedelta(days=1)
    return cand.timestamp()


def classify(text: str | None, *, event=None, assistant_error=None,
             now: float | None = None) -> Limit | None:
    """Account-wide subscription limit only; pure, no I/O."""
    now = time.time() if now is None else now
    info = getattr(event, "rate_limit_info", None)
    if info is not None and getattr(info, "status", None) == "rejected":
        typ = getattr(info, "rate_limit_type", None)
        if typ not in _ACCOUNT_WIDE:
            return None
        resets = getattr(info, "resets_at", None)
        if resets is None:
            resets = _parse_reset(text or "", now)
        return Limit(float(resets) if resets is not None else None, typ or "subscription")
    t = (text or "").lower()
    if not t or any(n in t for n in _NEVER):
        return None
    hit = ("hit your limit" in t or "session limit" in t or "usage limit reached" in t
           or ("limit reached" in t and "resets" in t))
    if not hit:
        return None
    return Limit(_parse_reset(text or "", now), "text")


# ---------------------------------------------------------------- key

def load_key() -> str | None:
    try:
        with open(env_file(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    key = None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith(KEY_NAME + "="):
            key = line.split("=", 1)[1].strip().strip("'\"").strip()
    return key or None


def redact(text):
    if not isinstance(text, str):
        return text
    key = load_key()
    return text.replace(key, "[redacted]") if key else text


# ---------------------------------------------------------------- state

_notifier = None
_last_denial: str | None = None


def set_notifier(fn) -> None:
    global _notifier
    _notifier = fn


def _notify(text: str) -> None:
    log.info("credits.notice %s", text)
    fn = _notifier
    if fn is not None:
        try:
            fn(text)
        except Exception:
            log.warning("credits.notifier_failed")


def last_denial() -> str | None:
    return _last_denial


def _today(now: float) -> str:
    return dt.datetime.fromtimestamp(now).date().isoformat()


def _defaults(now: float) -> dict:
    return {"until": 0, "day": _today(now), "spent_today": 0.0,
            "est_balance": start_balance(), "exhausted": False, "switched_at": 0,
            "reserved": [], "verified": False, "verified_date": "", "notice_key": ""}


@contextlib.contextmanager
def _locked(path: str | None = None, now: float | None = None):
    """Read-modify-write under flock; yields the dict, saves atomically on exit."""
    now = time.time() if now is None else now
    path = path or state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            s = _defaults(now)
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    s.update(loaded)
            except (OSError, ValueError):
                pass
            if s.get("day") != _today(now):
                s["day"], s["spent_today"] = _today(now), 0.0
            s["reserved"] = [r for r in (s.get("reserved") or [])
                             if isinstance(r, list) and len(r) == 3
                             and now - float(r[2]) < STALE_RESERVATION_S]
            yield s
            tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(s, fh)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def state(path: str | None = None, now: float | None = None) -> dict:
    """Read-only snapshot (does not write)."""
    now = time.time() if now is None else now
    s = _defaults(now)
    try:
        with open(path or state_path(), encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            s.update(loaded)
    except (OSError, ValueError):
        pass
    if s.get("day") != _today(now):
        s["day"], s["spent_today"] = _today(now), 0.0
    s["reserved"] = [r for r in (s.get("reserved") or [])
                     if isinstance(r, list) and len(r) == 3
                     and now - float(r[2]) < STALE_RESERVATION_S]
    return s


# ---------------------------------------------------------------- reservations

@dataclass(frozen=True)
class Reservation:
    id: str
    amount: float
    ts: float


@dataclass
class Route:
    env: dict = field(default_factory=dict, repr=False)
    denied: str | None = None
    reservation: Reservation | None = None


def _active() -> str | None:
    """Reason credits cannot be used at all (no notice), else None."""
    if not enabled():
        return "disabled"
    if KEY_NAME in os.environ:
        log.info("credits.already_on_key")
        return "already on key"
    return None


def _guard(s: dict, now: float) -> str | None:
    if s.get("exhausted"):
        return "credits exhausted"
    exp = effective_expiry()
    today = dt.datetime.fromtimestamp(now).date()
    midnight = dt.datetime.combine(exp, dt.time.min).timestamp()
    if today >= exp or now + STALE_RESERVATION_S >= midnight:
        return "expired"
    held = sum(float(r[1]) for r in s["reserved"])
    res = reserve_amount()
    if s["spent_today"] + held + res > daily_cap() + 1e-9:
        return "daily cap"
    if s["est_balance"] - held - res < floor() - 1e-9:
        return "floor"
    return None


def reserve(now: float | None = None, path: str | None = None) -> Reservation | None:
    """Atomic check-and-reserve. None when denied (see last_denial())."""
    global _last_denial
    now = time.time() if now is None else now
    if not enabled():
        _last_denial = "disabled"
        return None
    with _locked(path, now) as s:
        if not s.get("verified"):
            _last_denial = "unverified"
            return None
        if load_key() is None:
            _last_denial = "no key"
            return None
        why = _guard(s, now)
        if why:
            _last_denial = why
            return None
        r = Reservation(uuid.uuid4().hex[:12], reserve_amount(), now)
        s["reserved"].append([r.id, r.amount, r.ts])
        _last_denial = None
        return r


def release(reservation: Reservation | None, path: str | None = None) -> None:
    if reservation is None:
        return
    with _locked(path) as s:
        s["reserved"] = [r for r in s["reserved"] if r[0] != reservation.id]


def _estimate(usage: dict | None) -> float:
    if not usage:
        return reserve_amount()
    g = lambda k: float(usage.get(k) or 0)  # noqa: E731
    return (g("input_tokens") * _PRICE["input"] + g("output_tokens") * _PRICE["output"]
            + g("cache_read_input_tokens") * _PRICE["cache_read"]
            + g("cache_creation_input_tokens") * _PRICE["cache_creation"]) / 1e6


def record(total_cost_usd, reservation: Reservation | None, model: str | None = None,
           usage: dict | None = None, now: float | None = None,
           path: str | None = None) -> float:
    """Call with every ResultMessage of a call that ran on the key."""
    now = time.time() if now is None else now
    cost = (float(total_cost_usd) * MARGIN if total_cost_usd is not None
            else _estimate(usage) * MARGIN)
    with _locked(path, now) as s:
        if reservation is not None:
            s["reserved"] = [r for r in s["reserved"] if r[0] != reservation.id]
        s["spent_today"] = float(s["spent_today"]) + cost
        s["est_balance"] = float(s["est_balance"]) - cost
        log.info("credits.spend model=%s cost=%.2f day=%.2f est_balance=%.2f",
                 model, cost, s["spent_today"], s["est_balance"])
    return cost


# ---------------------------------------------------------------- routing

def _hhmm(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%H:%M")


def _stop_notice(s: dict, why: str, now: float) -> None:
    """Guard-stop notice, once per day/reason. Mutates s."""
    if why in ("unverified", "no key", "disabled", "already on key"):
        return
    nk = f"{s['day']}:{why}"
    if s.get("notice_key") == nk:
        return
    s["notice_key"] = nk
    detail = {"daily cap": f"daily cap ${daily_cap():g} reached",
              "floor": f"below ${floor():g} floor",
              "expired": f"expired {effective_expiry().isoformat()}",
              "credits exhausted": "exhausted"}.get(why, why)
    when = _hhmm(s["until"]) if s.get("until") else "the next reset"
    _notify(f"Subscription limit hit and API credits are paused ({detail}); "
            f"work resumes at {when} on the subscription.")


def _credits_route(now: float, path: str | None) -> Route:
    r = reserve(now, path)
    key = load_key() if r else None
    if r and key:
        return Route({KEY_NAME: key}, None, r)
    return Route({}, last_denial())


def route(now: float | None = None, path: str | None = None) -> Route:
    now = time.time() if now is None else now
    if _active():
        return Route()
    if state(path, now)["until"] <= now:
        return Route()
    rt = _credits_route(now, path)
    if rt.denied:
        with _locked(path, now) as s:
            _stop_notice(s, rt.denied, now)
    return rt


def on_limit(limit: Limit, now: float | None = None, path: str | None = None) -> Route | None:
    """A subscription call failed with a Limit. Returns the credits Route or None."""
    now = time.time() if now is None else now
    if _active():
        return None
    with _locked(path, now) as s:
        if not s.get("verified"):
            return None
        was_on = s["until"] > now
        if limit.reset_epoch is not None:
            until = min(max(limit.reset_epoch, now + 60), now + MAX_WINDOW_S)
        else:
            until = now + DEFAULT_WINDOW_S
        s["until"] = until
        if not was_on:
            s["switched_at"] = now
    rt = _credits_route(now, path)
    if rt.denied:
        with _locked(path, now) as s:
            _stop_notice(s, rt.denied, now)
        return None
    if not was_on:
        s = state(path, now)
        approx = "" if limit.reset_epoch is not None else " (about an hour)"
        _notify(f"Subscription limit hit, using API credits until {_hhmm(until)}{approx} "
                f"(est. ${s['est_balance']:.2f} left, today ${s['spent_today']:.2f} "
                f"of ${daily_cap():g})")
    return rt


def mark_exhausted(path: str | None = None) -> None:
    with _locked(path) as s:
        s["exhausted"], s["until"] = True, 0
    _notify("API credits exhausted, back on the subscription; I will not retry credits")


# ---------------------------------------------------------------- CLI

def _live_verify(key: str) -> dict:  # pragma: no cover - live, operator-run only
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions, query

    out: dict = {}

    async def go():
        opts = ClaudeAgentOptions(model="haiku", max_turns=1, env={KEY_NAME: key})
        async for m in query(prompt="Reply with the single word: ok", options=opts):
            data = getattr(m, "data", None)
            if isinstance(data, dict) and "apiKeySource" in data:
                out["api_key_source"] = data["apiKeySource"]
            if hasattr(m, "total_cost_usd"):
                out["total_cost_usd"] = m.total_cost_usd
    asyncio.run(go())
    return out


def main(argv: list[str] | None = None, query=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    now = time.time()
    if cmd == "status":
        s = state(now=now)
        print(f"day={s['day']} spent_today={s['spent_today']:.2f} "
              f"est_balance={s['est_balance']:.2f} until={s['until']} "
              f"exhausted={s['exhausted']} verified={s['verified']} "
              f"enabled={enabled()} expires={effective_expiry()}")
        return 0
    if cmd == "set-balance" and len(argv) == 2:
        try:
            v = float(argv[1])
        except ValueError:
            print("usage: set-balance <usd>")
            return 2
        with _locked(now=now) as s:
            s["est_balance"] = v
        return 0
    if cmd == "reset-exhausted":
        with _locked(now=now) as s:
            s["exhausted"] = False
        return 0
    if cmd == "verify":
        key = load_key()
        if not key:
            print("no key in env file")
            return 1
        try:
            res = (query or _live_verify)(key)
        except Exception as e:  # never print the key
            print("verify failed:", redact(type(e).__name__))
            return 1
        ok = res.get("api_key_source") == KEY_NAME
        with _locked(now=now) as s:
            s["verified"] = bool(ok)
            s["verified_date"] = _today(now) if ok else ""
        print(f"verified={ok} api_key_source={res.get('api_key_source')} "
              f"cost={res.get('total_cost_usd')}")
        return 0 if ok else 1
    print("usage: python -m navigator.credits status|set-balance X|reset-exhausted|verify")
    return 2


if __name__ == "__main__":
    sys.exit(main())
