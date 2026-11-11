"""Card-approved delayed restart of a launchd job (conductor or navigator).

The model never reaches this module with text of its own: the label comes from a fixed allowlist,
the delay is an int, the uid is os.getuid(). `schedule` has exactly one caller, the decide route in
server.py, after the operator approved the restart card.

Why a detached child: the plist has no AbandonProcessGroup, so launchd kills every process in the
job's process group when the job dies. A plain `nohup ... &` child shares that group and dies with
the restart; `start_new_session=True` (setsid) gives the child its own session and group, which the
group kill misses. UNVERIFIED on macOS 26: the first live restart (deploy step D3/D4) proves it.

Fallback if the detached child does not survive (do not build until D3 shows it is needed):
  (i)  conductor label: after the delay the conductor SIGTERMs itself from a timer thread started by
       schedule(), through the normal engine.stop() path; the plist KeepAlive respawns it after
       ThrottleInterval (30 s). No second process, so nothing for the group kill to miss.
  (ii) navigator label: a one-shot launchd job (`launchctl submit`, fixed label, argv built like
       command()), which launchd runs outside the conductor's group.
  (iii) `launchctl asuser` / `bsexec` do not help: they change the bootstrap context, not the group."""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

LABELS = {"conductor": "com.calwbot.conductor", "navigator": "com.calwbot.navigator"}
DEFAULT_DELAY_S = 10
MIN_DELAY_S, MAX_DELAY_S = 5, 60
GUARD_S = 120  # no second restart while a marker is this young
STALE_S = 600  # a marker older than this at startup is reported as stale
MARKER = "restart.json"
TITLE_MAX = 80


def enabled() -> bool:
    """Soft switch CONDUCTOR_RESTART_TOOL, read at call time: "0" turns the restart off."""
    return os.environ.get("CONDUCTOR_RESTART_TOOL", "1") != "0"


def clamp_delay(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return DEFAULT_DELAY_S
    return max(MIN_DELAY_S, min(MAX_DELAY_S, int(v)))


def precheck(helpers) -> list[dict]:
    """The helpers a restart would lose: running or queued, as id, title, age. A pure read."""
    if helpers is None:
        return []
    out = []
    for s in helpers.snapshot():
        if s.get("state") in ("running", "queued"):
            out.append({"id": _clean(s.get("id")), "title": _clean(s.get("title") or ""),
                        "age": float(s.get("age") or 0)})
    return out


def command(label: str, delay_s, uid: int) -> list[str]:
    """Built only from the allowlist, an int and a uid; a label outside LABELS raises."""
    if not isinstance(label, str) or label not in LABELS:
        raise ValueError(f"unknown restart label: {label!r}")
    return ["/bin/sh", "-c",
            f"sleep {int(delay_s)}; exec /bin/launchctl kickstart -k gui/{int(uid)}/{LABELS[label]}"]


def _read(root: Path) -> dict | None:
    """The marker as a dict, {} if it exists but is unreadable, None if absent."""
    p = Path(root) / MARKER
    try:
        raw = p.read_text()
    except OSError:
        return None if not p.exists() else {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def pending_marker(root: Path, clock=time.time) -> bool:
    """True while a restart is scheduled and young (the 120 s guard)."""
    m = _read(root)
    if m is None:
        return False
    at = _num(m.get("scheduled_at"), None)
    if at is None:
        try:
            at = (Path(root) / MARKER).stat().st_mtime
        except OSError:
            return False
    return clock() - at < GUARD_S


def _write_marker(root: Path, data: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".restart-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, root / MARKER)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def schedule(label: str, delay_s, root: Path, *, spawn=None, clock=time.time, pid=os.getpid,
             helpers: list[dict] | None = None, card_code: str | None = None, via: str | None = None,
             uid=None) -> dict:
    """Write the marker and start the detached sleep-then-kickstart child. Called only from the decide
    route, after the operator approved the card. The marker is written only here (never at startup)."""
    if not enabled():
        return {"ok": False, "reason": "restart is disabled (CONDUCTOR_RESTART_TOOL=0)"}
    if label not in LABELS:
        return {"ok": False, "reason": "unknown label"}
    if pending_marker(root, clock):
        return {"ok": False, "reason": "a restart is already scheduled"}
    delay = clamp_delay(delay_s)
    now, old_pid = clock(), pid()
    lost = [{"id": h["id"], "title": str(h.get("title") or "")[:TITLE_MAX]} for h in (helpers or [])]
    _write_marker(root, {"label": label, "scheduled_at": now, "delay_s": delay, "old_pid": old_pid,
                         "card_code": card_code, "via": via, "helpers_lost": lost})
    spawn = spawn or subprocess.Popen  # looked up at call time so a test guard can intercept it
    try:
        spawn(command(label, delay, os.getuid() if uid is None else uid), start_new_session=True,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        (Path(root) / MARKER).unlink(missing_ok=True)
        return {"ok": False, "reason": f"could not start the restart ({type(e).__name__})"}
    except BaseException:
        (Path(root) / MARKER).unlink(missing_ok=True)
        raise
    return {"ok": True, "at": now, "delay_s": delay, "old_pid": old_pid, "helpers": lost}


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _clean(v) -> str:
    return " ".join(str(v).split())[:TITLE_MAX]


def announce(root: Path, *, clock=time.time, pid=os.getpid) -> str | None:
    """At startup: the note for a planned restart, or None (no marker: an unplanned restart stays silent).
    Deletes the marker, except while the detached child may still be asleep (it would fire after an
    unrelated crash restart): then the marker stays, so the 120 s guard stays up, and None is returned."""
    m = _read(root)
    if m is None:
        return None
    now = clock()
    at = _num(m.get("scheduled_at"), None)
    if at is not None and now < at + _num(m.get("delay_s")) - 1:
        return None
    (Path(root) / MARKER).unlink(missing_ok=True)
    head = f"restarted at {_hhmm(now)} (pid {pid()})"
    if at is None:
        return head
    if now - at > STALE_S:
        return f"{head}; the scheduled restart marker from {_hhmm(at)} was stale"
    old = m.get("old_pid")
    if isinstance(old, int) and not isinstance(old, bool):
        head += f"; was pid {old}"
    lost = [h for h in (m.get("helpers_lost") or []) if isinstance(h, dict)]
    if lost:
        head += f"; {len(lost)} helper(s) lost: " + ", ".join(
            f"{_clean(h.get('id'))} {_clean(h.get('title'))}".strip() for h in lost)
    return head
