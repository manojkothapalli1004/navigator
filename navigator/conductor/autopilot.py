"""Autopilot (2026-11-02): when a turn finishes normally, queue a labeled "continue with the plan"
turn, so the conversation keeps working through the operator's plan file between messages.

Guardrails: OFF by default (state persists in the conductor's root); only a normal finish queues
the next turn (never after an error, timeout or /stop — /stop also switches it off; never while
an approval card is pending); caps per streak and per day switch it off; no turns in quiet hours
(23:00-07:00 ET) unless the operator allows them; the plan is one operator-editable markdown file
of `- [ ]` / `- [x]` items, and when every item is checked it switches itself off. Merges and
other gated actions are unchanged: they still stop for an approval card."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_CONSECUTIVE = 6
MAX_PER_DAY = 12
MAX_PARALLEL = 3  # helpers + builds running at once
QUIET_START, QUIET_END = 23, 7  # hours, America/New_York
ET = ZoneInfo("America/New_York")
PLAN_PATH = Path("/Users/Manoj/calwbot/logs/navigator/autopilot_plan.md")
_ITEM = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$")
_COMMANDS = {"autopilot on": "on", "autopilot off": "off", "autopilot status": "status",
             "autopilot quiet ok": "quiet_ok"}
_FILES_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:Files|Create|Modify|Test):\s*(.*)$", re.I)


@dataclass(frozen=True)
class WorkItem:
    """One running helper or running build; files=None means it did not declare them."""
    kind: str
    id: str
    files: "frozenset[str] | None" = None


def _norm(path: str) -> str:
    p = path.strip().strip("`").strip().removeprefix("./").rstrip("/")
    return "" if p == "." else p


def _overlap(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a.startswith(b + "/") or b.startswith(a + "/"))


def blocked_by(item_files, running, cap: int = MAX_PARALLEL) -> str | None:
    """Why an item must wait for the work in `running`, or None. Undeclared files count as overlap."""
    running = list(running or ())
    if len(running) >= cap:
        return f"{cap} helpers/builds already running"
    if not item_files:
        return "this item does not declare its files and work is running" if running else None
    for w in running:
        if not w.files:
            return f"running work {w.id} does not declare its files"
    for w in running:
        for f in sorted(item_files):
            if any(_overlap(f, g) for g in w.files):
                return f"overlaps {w.id} on {f}"
    return None


class Autopilot:
    def __init__(self, state_path: Path, plan_path: Path = PLAN_PATH, clock=None):
        self.state_path, self.plan_path = Path(state_path), Path(plan_path)
        self.clock = clock or (lambda: datetime.now(ET))
        self.state = {"on": False, "consecutive": 0, "day": "", "today": 0, "quiet_ok": False}
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            saved = None
        if isinstance(saved, dict):  # a damaged file must never stop the conductor from starting
            self.state["on"] = saved.get("on") is True
            self.state["quiet_ok"] = saved.get("quiet_ok") is True
            for k in ("consecutive", "today"):
                if isinstance(saved.get(k), int) and saved[k] >= 0:
                    self.state[k] = saved[k]
            if isinstance(saved.get("day"), str):
                self.state["day"] = saved["day"]

    # ---- state ----
    @property
    def on(self) -> bool:
        return bool(self.state["on"])

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def _roll_day(self) -> None:
        day = self.clock().astimezone(ET).date().isoformat()
        if self.state["day"] != day:
            self.state["day"], self.state["today"] = day, 0

    def set(self, on: bool) -> None:
        self._roll_day()
        self.state["on"] = bool(on)
        self.state["consecutive"] = 0
        if not on:
            self.state["quiet_ok"] = False  # "unless the operator says so" lasts until it's switched off
        self._save()

    def allow_quiet(self, ok: bool = True) -> None:
        self.state["quiet_ok"] = bool(ok)
        self._save()

    def started(self) -> None:
        """An autopilot turn was queued."""
        self._roll_day()
        self.state["consecutive"] += 1
        self.state["today"] += 1
        self._save()

    def operator_turn(self) -> None:
        """The operator spoke: the in-a-row streak starts again."""
        if self.state["consecutive"]:
            self.state["consecutive"] = 0
            self._save()

    # ---- plan ----
    def next_item(self) -> str | None:
        """First unchecked item; None when all are checked. Raises OSError when the file is missing."""
        for line in self.plan_path.read_text().splitlines():
            m = _ITEM.match(line)
            if m and m.group(1) == " ":
                return m.group(2)
        return None

    def next_item_files(self) -> "frozenset[str] | None":
        """Files the first unchecked item declares (its `Files:` / `- Create:` / `- Modify:` / `- Test:` lines
        under it, comma separated, `:line-range` stripped); None when it declares none or the plan is unreadable."""
        try:
            lines = self.plan_path.read_text().splitlines()
        except OSError:
            return None
        found, out = False, set()
        for line in lines:
            m = _ITEM.match(line)
            if m:
                if found:
                    break
                found = m.group(1) == " "
                continue
            if found and (f := _FILES_LINE.match(line)):
                for part in f.group(1).split(","):
                    part = re.sub(r":\d+(-\d+)?$", "", part.strip().strip("`").strip())
                    if part:
                        out.add(part)
        return frozenset(out) or None

    def _blocked(self, work_running, item_files, running) -> str | None:
        if running:
            return blocked_by(item_files if item_files is not None else self.next_item_files(), running)
        if work_running:
            return "a build or helper is still running"
        return None

    # ---- the decision after every turn ----
    def decide(self, outcome: str, cards_pending: bool, work_running: bool = False,
               item_files=None, running=()) -> tuple[str, str | None]:
        """("idle", None) off | ("off", reason) switched itself off | ("wait", reason) on but not now |
        ("queue", item) queue a turn for this plan item."""
        if not self.on:
            return "idle", None
        if outcome == "cancelled":
            self.set(False)
            return "off", "stopped by the operator (/stop)"
        if outcome != "done":
            return "wait", f"the last turn ended with {outcome}; waiting for you"
        if cards_pending:
            return "wait", "an approval card is waiting for you"
        if reason := self._blocked(work_running, item_files, running):
            return "wait", reason
        self._roll_day()
        if self.state["today"] >= MAX_PER_DAY:
            self.set(False)
            return "off", f"{MAX_PER_DAY} autopilot turns today (the daily cap)"
        if self.state["consecutive"] >= MAX_CONSECUTIVE:
            self.set(False)
            return "off", f"{MAX_CONSECUTIVE} autopilot turns in a row (say 'autopilot on' to continue)"
        if self._quiet():
            return "wait", "quiet hours (23:00-07:00 ET); say 'autopilot quiet ok' to allow"
        try:
            item = self.next_item()
        except OSError:
            self.set(False)
            return "off", f"no plan file at {self.plan_path}"
        if item is None:
            self.set(False)
            return "off", "the plan is done — every item is checked"
        return "queue", item

    def _quiet(self) -> bool:
        hour = self.clock().astimezone(ET).hour
        return (hour >= QUIET_START or hour < QUIET_END) and not self.state["quiet_ok"]

    def runnable(self, cards_pending: bool, work_running: bool = False, item_files=None, running=()) -> str | None:
        """Re-checked when a queued autopilot turn is about to start: None = go, else why not."""
        if not self.on:
            return "autopilot is off"
        if cards_pending:
            return "an approval card is waiting for you"
        if reason := self._blocked(work_running, item_files, running):
            return reason
        if self._quiet():
            return "quiet hours (23:00-07:00 ET)"
        return None

    def public(self) -> dict:
        self._roll_day()
        return {"on": self.on, "consecutive": self.state["consecutive"], "today": self.state["today"],
                "quiet_ok": bool(self.state["quiet_ok"]), "max_consecutive": MAX_CONSECUTIVE,
                "max_per_day": MAX_PER_DAY, "plan": str(self.plan_path)}

    @staticmethod
    def command(text: str) -> str | None:
        """The operator's exact switch words; anything else is an ordinary message."""
        return _COMMANDS.get(" ".join(text.lower().split()))

    def prompt(self, item: str) -> str:
        return (f"Continue with the plan in {self.plan_path}. Next unchecked item: \"{item}\". Work on it "
                "with the usual rules (own branch + worktree, tests first, one cold audit, ledger; merges, "
                "trader/money paths and other gated actions go through approval cards — never around them). "
                "When the item is finished, mark it [x] in the plan file and say in one line what you did. "
                "If it needs a decision only the operator can make, say so and stop.")
