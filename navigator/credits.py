"""Subscription -> API-credits fallback policy (single owner; sync, stdlib only).

The key is read from the .env file at call time, never cached, never logged,
never stored in state, and only ever handed out inside ``Route.env``
(hidden from repr). Every call on the key carries a dollar budget (``Route.budget_usd``,
passed to the CLI as --max-budget-usd) equal to its reservation, so the daily cap and the
floor bound real spend, not just the count of calls. A subscription limit is confirmed by a
probe (one haiku turn on the subscription) before anything switches to credits: credits are
used only when the subscription really is out. Every spend, switch, denial and probe is
appended to an events file (NAVIGATOR_CREDITS_LOG) that never contains the key.
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
STALE_RESERVATION_S = 50 * 60  # above the 45-min conductor turn timeout: a live call's hold never lapses
KEY_NAME = "ANTHROPIC_API_KEY"
PROBE_MODEL = "haiku"  # CLI alias, as _live_verify uses; a wrong id would make every probe "unknown"
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_REUSE_S = 60
PROBE_TIMEOUT_S = 60.0

# $/Mtok (input, output, cache_read, cache_creation) by model-name substring; unknown = opus (conservative)
_PRICES = {"haiku": (1.0, 5.0, 0.1, 1.25), "sonnet": (3.0, 15.0, 0.3, 3.75), "opus": (15.0, 75.0, 1.5, 18.75)}


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


def call_max() -> float:
    """Most one call on the key may spend (its --max-budget-usd); also its reservation."""
    return _f("CREDITS_CALL_MAX_USD", 5.0)


def call_min() -> float:
    """Below this much headroom a call is refused rather than run with a useless budget."""
    return _f("CREDITS_CALL_MIN_USD", 0.5)


def events_path() -> str:
    return os.environ.get("NAVIGATOR_CREDITS_LOG") or "/Users/Manoj/calwbot/logs/navigator/credits_events.jsonl"


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
    try:
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
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


def _event(kind: str, **fields) -> None:
    """Append one JSON line to the events file (0600). Never raises; never holds the key."""
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
    try:
        line = redact(json.dumps(rec, default=str))
        path = events_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        log.warning("credits.event_failed kind=%s", kind)


def _tokens(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    g = lambda k: usage.get(k) or 0  # noqa: E731
    return {"input": g("input_tokens"), "output": g("output_tokens"),
            "cache_read": g("cache_read_input_tokens"), "cache_creation": g("cache_creation_input_tokens")}


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
            "reserved": [], "verified": False, "verified_date": "", "notice_key": "",
            "probe_at": 0, "probe_result": "", "probe_notices": {}}


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
    budget_usd: float | None = None  # --max-budget-usd for this call; None off the key


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
    return _budget(s)[1]


def _budget(s: dict) -> tuple[float | None, str | None]:
    """(budget for one more call, None) or (None, reason). The budget is what the call may spend:
    min(call_max, daily headroom, balance headroom above the floor) net of live reservations."""
    held = sum(float(r[1]) for r in s["reserved"])
    spent_room = daily_cap() - float(s["spent_today"])
    bal_room = float(s["est_balance"]) - floor()
    if spent_room < call_min() - 1e-9:
        return None, "daily cap"          # really spent: worth a notice
    if bal_room < call_min() - 1e-9:
        return None, "floor"
    daily, bal = spent_room - held, bal_room - held
    if min(daily, bal) < call_min() - 1e-9:
        return None, "busy"               # headroom exists but in-flight calls hold it: this call waits, no notice
    return round(min(call_max(), daily, bal), 4), None


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
        budget, _ = _budget(s)
        r = Reservation(uuid.uuid4().hex[:12], float(budget), now)
        s["reserved"].append([r.id, r.amount, r.ts])
        _last_denial = None
        return r


def release(reservation: Reservation | None, path: str | None = None) -> None:
    if reservation is None:
        return
    with _locked(path) as s:
        s["reserved"] = [r for r in s["reserved"] if r[0] != reservation.id]


def _prices(model: str | None) -> tuple[float, float, float, float]:
    m = (model or "").lower()
    for name, p in _PRICES.items():
        if name in m:
            return p
    return _PRICES["opus"]


def _estimate(usage: dict | None, model: str | None = None, fallback: float | None = None) -> float:
    """Token-priced estimate for `model`; with no usage, the most the call could have spent."""
    if not usage:
        return call_max() if fallback is None else float(fallback)
    inp, out, cr, cc = _prices(model)
    g = lambda k: float(usage.get(k) or 0)  # noqa: E731
    return (g("input_tokens") * inp + g("output_tokens") * out
            + g("cache_read_input_tokens") * cr + g("cache_creation_input_tokens") * cc) / 1e6


def record(total_cost_usd, reservation: Reservation | None, model: str | None = None,
           usage: dict | None = None, now: float | None = None,
           path: str | None = None) -> float:
    """Call with every ResultMessage of a call that ran on the key."""
    now = time.time() if now is None else now
    if total_cost_usd is not None:
        cost = float(total_cost_usd) * MARGIN
    elif not usage and reservation is not None:
        cost = float(reservation.amount)  # no result at all: the hold is already the most it could have spent
    else:
        cost = _estimate(usage, model) * MARGIN
    capped = False
    notice: tuple | None = None
    with _locked(path, now) as s:
        if reservation is not None:
            s["reserved"] = [r for r in s["reserved"] if r[0] != reservation.id]
        s["spent_today"] = float(s["spent_today"]) + cost
        s["est_balance"] = float(s["est_balance"]) - cost
        if s["spent_today"] >= daily_cap() - 1e-9 and float(s.get("until") or 0) > now:
            s["until"] = 0  # the cap is spent: close the window so nothing else routes to the key today
            capped = True
            nk = f"{s['day']}:daily cap"
            if s.get("notice_key") != nk:
                s["notice_key"] = nk
                notice = ("daily cap",)
        spent, bal = s["spent_today"], s["est_balance"]
        log.info("credits.spend model=%s cost=%.2f day=%.2f est_balance=%.2f", model, cost, spent, bal)
    if notice:  # outside the lock: the notifier may talk to Telegram
        _notify(f"API credits daily cap ${daily_cap():g} reached; back on the subscription until tomorrow.")
    _event("spend", model=model, cost=round(cost, 4), total_cost_usd=total_cost_usd, tokens=_tokens(usage),
           reservation=reservation.id if reservation is not None else None,
           spent_today=round(spent, 4), est_balance=round(bal, 4))
    if capped:
        _event("cap_reached", spent_today=round(spent, 4), cap=daily_cap())
    return cost


# ---------------------------------------------------------------- routing

def _hhmm(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%H:%M")


def _stop_notice(s: dict, why: str, now: float) -> None:
    """Guard-stop notice, once per day/reason. Mutates s."""
    if why in ("unverified", "no key", "disabled", "already on key", "busy"):
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
        return Route({KEY_NAME: key}, None, r, round(r.amount / MARGIN, 4))
    if r:  # reserved but the key vanished from the env file: give the hold back
        release(r, path)
        _last_denial_set("no key")
    _event("denied", reason=last_denial())
    return Route({}, last_denial())


def _last_denial_set(why: str | None) -> None:
    global _last_denial
    _last_denial = why


def route(now: float | None = None, path: str | None = None) -> Route:
    now = time.time() if now is None else now
    if _active():
        return Route()
    s = state(path, now)
    if s["until"] <= now:
        return Route()
    # the window is open: keep checking (one probe a minute at most, and only when a call could actually run on
    # the key) that the subscription is still out, so a window opened by mistake, or a subscription that recovered
    # early, never spends credits. Async callers use route_async(): the probe blocks for seconds.
    if _guard(s, now) is None:
        res = _confirm(now, path)
        if res == "ok":
            with _locked(path, now) as s:
                s["until"] = 0
            _event("window_closed", reason="probe ok")
            _unconfirmed_notice("recovered", now, path)
            return Route()
        if res in ("unknown", "pending"):
            return Route()  # fail closed: unconfirmed means no credits
    rt = _credits_route(now, path)
    if rt.denied:
        with _locked(path, now) as s:
            _stop_notice(s, rt.denied, now)
    return rt


def _probe_runner(timeout_s: float = PROBE_TIMEOUT_S) -> tuple[str, str]:  # pragma: no cover - live
    """ONE haiku turn on the SUBSCRIPTION through the Agent SDK (no key: when this runs the key is not
    in os.environ, see _active()). Runs in a worker thread with its own loop because callers sit inside
    a running loop. Returns ("ok" | "limited" | "unknown", text)."""
    import threading

    out: dict = {}

    def run() -> None:
        try:
            import asyncio

            from claude_agent_sdk import ClaudeAgentOptions, query

            async def go() -> None:
                opts = ClaudeAgentOptions(model=PROBE_MODEL, max_turns=1, setting_sources=[],
                                          strict_mcp_config=True, mcp_servers={}, tools=[], allowed_tools=[],
                                          cwd=os.path.dirname(state_path()) or ".")
                async for m in query(prompt=PROBE_PROMPT, options=opts):
                    _probe_observe(out, m)
            asyncio.run(asyncio.wait_for(go(), timeout_s))
        except Exception as e:  # noqa: BLE001
            out["exc"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=run, name="credits-probe", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return "unknown", "probe timed out"
    return _probe_interpret(out)


def _probe_observe(out: dict, m) -> None:
    """Collect what one SDK message says about the probe (pure bookkeeping on `out`)."""
    if classify(None, event=m) is not None:          # account-wide rejected rate-limit event only
        out["limited"] = True
    if getattr(m, "error", None) == "rate_limit":
        out["limited"] = True
    data = getattr(m, "data", None)
    if isinstance(data, dict) and data.get("apiKeySource") not in (None, "none"):
        out["on_key"] = True                           # not the subscription: inconclusive
    if type(m).__name__ == "ResultMessage":
        out["is_error"] = bool(getattr(m, "is_error", False))
        out["text"] = str(getattr(m, "result", None) or getattr(m, "subtype", "") or "")


def _probe_interpret(out: dict) -> tuple[str, str]:
    """("ok" | "limited" | "unknown", text). A successful answer always wins: the subscription works."""
    if out.get("on_key"):
        return "unknown", "probe ran on a key, not the subscription"
    if out.get("is_error") is False:
        return "ok", out.get("text", "")
    if out.get("limited"):
        return "limited", out.get("text") or "rate limit event"
    if "exc" in out:
        return ("limited" if classify(out["exc"]) else "unknown"), out["exc"]
    if "is_error" not in out:
        return "unknown", "probe returned no result"
    text = out.get("text", "")
    return ("limited" if classify(text) else "unknown"), text


def _confirm(now: float, path: str | None) -> str:
    """Is the subscription really out? "limited" (switch), "ok"/"unknown" (stay), "skipped" (CREDITS_PROBE=0).
    One probe per PROBE_REUSE_S; the stored result is reused inside that window."""
    if os.environ.get("CREDITS_PROBE", "1") == "0":
        return "skipped"
    with _locked(path, now) as s:
        if s.get("probe_result") and now - float(s.get("probe_at") or 0) < PROBE_REUSE_S:
            res = str(s["probe_result"])
            if res != "pending":
                _event("probe", result=res, reused=True)
            return res  # "pending": another caller is probing right now; this one stays on the subscription
        s["probe_at"], s["probe_result"] = now, "pending"  # claim, then probe outside the lock
    t0 = time.time()
    try:
        res, text = _probe_runner()
    except Exception as e:  # noqa: BLE001
        res, text = "unknown", f"{type(e).__name__}: {e}"
    if res not in ("ok", "limited", "unknown"):
        res = "unknown"
    with _locked(path, now) as s:
        s["probe_at"], s["probe_result"] = now, res
    _event("probe", result=res, ms=int((time.time() - t0) * 1000), text=redact(str(text))[:200])
    return res


def _unconfirmed_notice(res: str, now: float, path: str | None) -> None:
    """Once per day per result: the operator learns why a reported limit did not switch to credits."""
    day = _today(now)
    with _locked(path, now) as s:
        notices = s.get("probe_notices") if isinstance(s.get("probe_notices"), dict) else {}
        if notices.get(res) == day:
            return
        notices[res] = day
        s["probe_notices"] = notices
    if res == "ok":
        _notify("A subscription limit was reported, but a probe shows the subscription still works; "
                "staying on it (no API credits).")
    elif res == "recovered":
        _notify("The subscription answers again; back on it (API credits window closed).")
    else:
        _notify("A subscription limit was reported and the probe could not confirm it; staying on the "
                "subscription (no API credits). Check `navigator.credits events`.")


def on_limit(limit: Limit, now: float | None = None, path: str | None = None) -> Route | None:
    """A subscription call failed with a Limit. Returns the credits Route or None.
    Switching (opening the window) happens only after the probe confirms the subscription is out."""
    now = time.time() if now is None else now
    if _active():
        return None
    pre = state(path, now)
    if not pre.get("verified"):
        return None
    if pre["until"] <= now:  # not on credits yet: confirm before switching
        res = _confirm(now, path)
        if res in ("ok", "unknown", "pending"):
            global _last_denial
            _last_denial = f"probe {res}"
            log.info("credits.limit_unconfirmed probe=%s", res)
            if res != "pending":
                _unconfirmed_notice(res, now, path)
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
        _event("switch", until=until, limit_kind=limit.kind, budget=rt.budget_usd,
               est_balance=round(float(s["est_balance"]), 4), spent_today=round(float(s["spent_today"]), 4))
        _notify(f"Subscription limit hit, using API credits until {_hhmm(until)}{approx} "
                f"(est. ${s['est_balance']:.2f} left, today ${s['spent_today']:.2f} "
                f"of ${daily_cap():g})")
    return rt


async def _threaded(fn, *args, path: str | None = None):
    """Run fn(*args) in a worker thread (it may probe the subscription for seconds). If the awaiting task is
    cancelled while the worker still runs, the Route the worker eventually returns is released, so no hold
    is orphaned."""
    import asyncio

    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def _drop(t) -> None:
            if not t.cancelled() and t.exception() is None:
                r = t.result()
                if r is not None and getattr(r, "reservation", None) is not None:
                    release(r.reservation, path)
        task.add_done_callback(_drop)
        raise


async def on_limit_async(limit: Limit, path: str | None = None) -> Route | None:
    """on_limit() off the event loop; cancel-safe (see _threaded)."""
    return await _threaded(on_limit, limit, None, path, path=path)


async def route_async(path: str | None = None) -> Route:
    """route() off the event loop (it may probe while a window is open); cancel-safe (see _threaded)."""
    return await _threaded(route, None, path, path=path)


def mark_exhausted(path: str | None = None) -> None:
    with _locked(path) as s:
        s["exhausted"], s["until"] = True, 0
    _event("exhausted")
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
        held = sum(float(r[1]) for r in s["reserved"])
        print(f"day={s['day']} spent_today={s['spent_today']:.2f} "
              f"est_balance={s['est_balance']:.2f} until={s['until']} "
              f"exhausted={s['exhausted']} verified={s['verified']} "
              f"enabled={enabled()} expires={effective_expiry()} "
              f"held={held:.2f} reservations={len(s['reserved'])} call_max={call_max():g} "
              f"probe={s.get('probe_result') or '-'} probe_at={s.get('probe_at') or 0}")
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
    if cmd == "events":
        try:
            n = max(1, int(argv[1])) if len(argv) > 1 else 20
        except ValueError:
            print("usage: events [N]")
            return 2
        try:
            with open(events_path(), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        for line in lines[-n:]:
            print(line)
        print(f"({len(lines)} events in {events_path()})")
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
        _event("verify", ok=bool(ok), api_key_source=res.get("api_key_source"), total_cost_usd=res.get("total_cost_usd"))
        print(f"verified={ok} api_key_source={res.get('api_key_source')} "
              f"cost={res.get('total_cost_usd')}")
        return 0 if ok else 1
    print("usage: python -m navigator.credits status|set-balance X|reset-exhausted|verify|events [N]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
