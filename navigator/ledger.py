"""Completion ledger (2026-11-09): an append-only JSON-lines record of what helpers and builds actually did
(tools used, files written, commands run, cost, state changes), so a report's claims can be checked.

No conductor imports at module level (it cannot cycle). Writers: the conductor process only; the CLI reads.
Every free-text field goes through scrub(): redacted BEFORE it is truncated. Never holds task or result text.
CONDUCTOR_LEDGER=0 turns writing off (read at call time); reading still works."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

_log = logging.getLogger(__name__)

PATH = Path("/Users/Manoj/calwbot/logs/navigator/ledger.jsonl")  # tests repoint it (tests/conftest.py)
MAX_BYTES = 5_000_000
LINE_MAX = 4000
MAX_FILES, FILE_CHARS = 50, 200
MAX_COMMANDS, CMD_CHARS = 30, 80
TITLE_CHARS = 120
_LOCK = threading.Lock()


def scrub(s, n: int) -> str:
    """Secrets out first, then whitespace collapsed, then cut to n (so a token at the cut is never half shown)."""
    from navigator import credits
    from navigator.conductor.activity import _SECRET
    t = _SECRET.sub("…", credits.redact(str(s if s is not None else "")))
    return " ".join(t.split())[:n]


def count_unverified(text) -> int:
    return len(re.findall(r"(?im)^\s*[-*]?\s*UNVERIFIED\b", text or ""))


class Tally:
    """What one helper did, fed by ledger_step / ledger_cost events."""

    def __init__(self) -> None:
        self.tools: Counter = Counter()
        self.files: list[str] = []
        self.commands: list[str] = []
        self.cost_usd: float | None = None

    def step(self, tool, path=None, cmd=None) -> None:
        self.tools[str(tool)[:40]] += 1
        if path and len(self.files) < MAX_FILES:
            f = scrub(path, FILE_CHARS)
            if f and f not in self.files:
                self.files.append(f)
        if cmd and len(self.commands) < MAX_COMMANDS:
            self.commands.append(scrub(cmd, CMD_CHARS))

    def cost(self, usd) -> None:
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            self.cost_usd = float(usd)

    def record(self, *, id, title, state, started, ended, unverified: int = 0, card: bool = False) -> dict:
        rec = {"v": 1, "kind": "helper", "id": id, "title": scrub(title, TITLE_CHARS), "state": state,
               "started": started, "ended": ended, "tools": dict(self.tools), "files": list(self.files),
               "commands": list(self.commands), "cost_usd": self.cost_usd, "unverified": unverified}
        if card:
            rec["card"] = True
        return rec


def _encode(rec: dict) -> bytes:
    def dump(r) -> bytes:
        return json.dumps(r, separators=(",", ":"), ensure_ascii=True, default=str).encode("ascii") + b"\n"
    rec = dict(rec)
    raw = dump(rec)
    for key in ("commands", "files"):
        while len(raw) > LINE_MAX and rec.get(key):
            rec[key] = list(rec[key])[:-1]
            raw = dump(rec)
    while len(raw) > LINE_MAX and rec.get("title"):
        rec["title"] = rec["title"][:max(0, len(rec["title"]) // 2)]
        raw = dump(rec)
    return raw


def append(rec: dict, path=None) -> None:
    """One compact line, one os.write on an O_APPEND fd. Never raises."""
    if os.environ.get("CONDUCTOR_LEDGER", "1") == "0":
        return
    try:
        p = Path(path if path is not None else PATH)  # PATH read at call time
        raw = _encode(rec)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _LOCK:
            lock_fd = os.open(p.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    if p.stat().st_size >= MAX_BYTES:
                        os.replace(p, str(p) + ".1")
                except FileNotFoundError:
                    pass
                fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
            finally:
                os.close(lock_fd)  # closing releases the flock
    except Exception as e:  # noqa: BLE001
        _log.warning("navigator.ledger_append_failed: %s", type(e).__name__)


def _when(r: dict) -> float:
    for k in ("ended", "ts", "started"):
        if isinstance(r.get(k), (int, float)):
            return float(r[k])
    return 0.0


def read(since=None, path=None, limit: int | None = None) -> list[dict]:
    p = Path(path if path is not None else PATH)
    out: list[dict] = []
    for f in (Path(str(p) + ".1"), p):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and (since is None or _when(r) >= since):
                out.append(r)
    return out[-limit:] if limit else out


def seen_ids(path=None) -> set[str]:
    return {str(r.get("id")) for r in read(None, path) if r.get("id")}


def table(rows: list[dict]) -> str:
    head = f"{'when':<11} {'kind':<6} {'id':<18} {'state':<9} {'dur':>6} {'tools':>5} {'files':>5} {'cmds':>4} " \
           f"{'cost':>6} {'UNV':>3}  title"
    lines = [head]
    for r in rows:
        w = _when(r)
        dur = (r["ended"] - r["started"]) if isinstance(r.get("ended"), (int, float)) \
            and isinstance(r.get("started"), (int, float)) else None
        cost = r.get("cost_usd")
        lines.append(
            f"{time.strftime('%m-%d %H:%M', time.localtime(w)) if w else '-':<11} {str(r.get('kind', ''))[:6]:<6} "
            f"{str(r.get('id', ''))[:18]:<18} {str(r.get('state', ''))[:9]:<9} "
            f"{(f'{dur:.0f}s' if dur is not None else '-'):>6} {sum((r.get('tools') or {}).values()):>5} "
            f"{len(r.get('files') or []):>5} {len(r.get('commands') or []):>4} "
            f"{(f'{cost:.2f}' if isinstance(cost, (int, float)) else '-'):>6} {r.get('unverified', 0) or 0:>3}  "
            f"{str(r.get('title', ''))[:40]}")
    return "\n".join(lines)
