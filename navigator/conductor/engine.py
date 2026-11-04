"""One conversation, one turn at a time. Every window submits into the same FIFO; one worker
runs the turns against the persisted Agent SDK session and publishes events to subscribers."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from navigator.conductor import activity
from navigator.conductor.store import Store

_log = logging.getLogger(__name__)

SOURCES = ("web", "telegram", "terminal")
TERMINAL_WAIT = "waiting for the terminal session to end"
TURN_TIMEOUT_S = 45 * 60  # a stuck turn (tool waiting on input, network stall) must not freeze every window
TurnRunner = Callable[[str, "str | None", Callable[[dict], None]], Awaitable["str | None"]]


class ResumeFailed(Exception):
    """The stored session id could not be resumed (deleted or corrupt transcript)."""


class Engine:
    def __init__(self, store: Store, runner: TurnRunner, lock_path: Path | None = None,
                 turn_timeout_s: float = TURN_TIMEOUT_S, notify: Callable[[dict], None] | None = None,
                 reply: Callable[[str], None] | None = None, autopilot=None,
                 cards_pending: Callable[[], bool] | None = None):
        self.store, self.runner, self.turn_timeout_s = store, runner, turn_timeout_s
        self.autopilot = autopilot  # keeps working through the plan file between turns (off by default)
        self.cards_pending = cards_pending or (lambda: False)
        self.notify = notify  # tells the operator's phone about a new approval card (C3)
        self.reply = reply  # sends a Telegram-originated turn's outcome back to Telegram (C4)
        self._dropped: set[int] = set()
        self.lock_path = Path(lock_path) if lock_path else store.root / "turn.lock"
        self._pending: asyncio.Queue | None = None
        self._subs: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._cancelled = False
        self._cancel_via: str | None = None
        self.busy = False
        self.terminal = False  # the operator joined from the terminal (C5)
        # the latest turn's tool steps, for crew screens in a window that opens mid-turn (in memory only)
        self._activity_msg: str | None = None
        self._activity: dict[str, dict] = {}

    def activity_snapshot(self) -> dict:
        return {"msg_id": self._activity_msg, "items": list(self._activity.values())}

    def _on_activity(self, msg_id: str, ev: dict) -> None:
        if msg_id != self._activity_msg:
            self._activity_msg, self._activity = msg_id, {}
        item = self._activity.get(ev.get("id", ""))
        if item is None:
            if ev.get("state") != "running":
                return  # a result whose start was never seen
            item = {k: ev.get(k) for k in ("id", "tool", "title", "parent", "helper")}
            item["state"] = "running"  # set before the cap runs, so the new step isn't the one evicted
            self._activity[item["id"]] = item
            while len(self._activity) > activity.MAX_ITEMS:
                # the oldest finished plain step goes first; helpers and running steps are kept
                old = next((k for k, v in self._activity.items()
                            if v.get("state") != "running" and not v.get("helper")), next(iter(self._activity)))
                self._activity.pop(old)
        item["state"] = ev.get("state")
        item["ts"] = datetime.now(timezone.utc).isoformat()
        self._publish({"type": "activity", "msg_id": msg_id, "item": dict(item)})

    def _close_activity(self, msg_id: str, state: str) -> None:
        """A turn ended: no step of it may keep saying it runs (Stop, timeout, error, or a lost result)."""
        if msg_id != self._activity_msg:
            return
        for item_id, item in list(self._activity.items()):
            if item.get("state") == "running":
                self._on_activity(msg_id, {"type": "activity", "id": item_id, "state": state})

    @property
    def queue_len(self) -> int:
        return self._pending.qsize() if self._pending else 0

    async def start(self) -> None:
        self._pending = asyncio.Queue()
        self._mark_interrupted()
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def is_dropped(self, q: asyncio.Queue) -> bool:
        """The engine gave up on this subscriber (it fell behind); its stream must end so the
        window reconnects and re-reads /history."""
        return id(q) in self._dropped

    def cancel(self, via: str | None = None) -> bool:
        """Stop the running turn (the operator's Stop button / Telegram /stop); False when nothing
        runs. via="telegram": the bot already answered "Stopped.", so no second Telegram reply."""
        if self._turn_task is None or self._turn_task.done():
            return False
        self._cancelled = True
        self._cancel_via = via
        self._turn_task.cancel()
        return True

    async def _run(self, prompt: str, sid: str | None, emit) -> str | None:
        # the runner call is its own task so cancel() stops it without stopping the worker
        self._turn_task = asyncio.ensure_future(asyncio.wait_for(self.runner(prompt, sid, emit),
                                                                 self.turn_timeout_s))
        try:
            return await self._turn_task
        finally:
            self._turn_task = None

    def submit(self, text: str, source: str, internal: bool = False,
               reply_via: str | None = None) -> dict:
        # "approval" / "autopilot" messages come only from the conductor itself
        if source not in SOURCES and not (internal and source in ("approval", "autopilot")):
            raise ValueError(f"unknown source {source!r}")
        if self.autopilot is not None and source in SOURCES:
            if cmd := self.autopilot.command(text):
                return self._autopilot_command(cmd, source)  # a switch, never a turn
            self.autopilot.operator_turn()
        user = self._append({"kind": "user", "text": text, "source": source,
                             **({"reply_via": reply_via} if reply_via else {})})
        ahead = self.queue_len + (1 if self.busy else 0)
        detail = TERMINAL_WAIT if self.terminal else ahead
        self._append({"kind": "status", "state": "queued", "msg_id": user["id"], "detail": detail})
        if self.terminal and source != "autopilot":
            self._reply(user, "You're in the terminal — I'll answer this when you leave it.")
        self._pending.put_nowait(user)
        return user

    def _autopilot_command(self, cmd: str, source: str) -> dict:
        ap = self.autopilot
        if cmd == "on":
            ap.set(True)
        elif cmd == "off":
            ap.set(False)
        elif cmd == "quiet_ok":
            ap.allow_quiet(True)
        st = ap.public()
        text = (f"Autopilot {'on' if st['on'] else 'off'} (via {source}) — {st['consecutive']}/{st['max_consecutive']} "
                f"in a row, {st['today']}/{st['max_per_day']} today{', quiet hours allowed' if st['quiet_ok'] else ''}; "
                f"plan: {st['plan']}")
        note = self._append({"kind": "note", "text": text})
        self._reply({"source": source}, text)
        return note

    def _after_turn(self, outcome: str) -> None:
        """Autopilot: decide whether to queue the next plan item now that a turn ended."""
        if self.autopilot is None:
            return
        if outcome != "cancelled" and self.queue_len > 0:
            return  # something is already waiting (an operator message): decide after it, never pile up
        try:
            kind, what = self.autopilot.decide(outcome, cards_pending=bool(self.cards_pending()))
        except Exception as e:  # the worker must outlive any autopilot hiccup
            _log.warning("conductor.autopilot_failed: %s", type(e).__name__)
            return
        if kind == "off":
            text = f"Autopilot turned off: {what}"
            self._append({"kind": "note", "text": text})
            self._send_telegram(text)
        elif kind == "queue":
            self.autopilot.started()
            st = self.autopilot.public()
            self._send_telegram(f"Autopilot: turn {st['consecutive']}/{st['max_consecutive']} — {what}")
            self.submit(self.autopilot.prompt(what), "autopilot", internal=True, reply_via="telegram")

    def _send_telegram(self, text: str) -> None:
        if self.reply:
            try:
                self.reply(text)
            except Exception as e:
                _log.warning("conductor.telegram_reply_failed: %s", type(e).__name__)

    def terminal_joined(self) -> None:
        self.terminal = True
        self._append({"kind": "note", "text": "Operator is in the terminal — replies wait until they leave."})

    def terminal_left(self, entries: list[dict]) -> int:
        """The terminal session ended: show its exchanges in every window (never run as turns)."""
        for e in entries:
            if e["kind"] == "user":
                self._append({"kind": "user", "text": e["text"], "source": "terminal", "imported": True})
            else:
                self._append({"kind": "assistant", "text": e["text"], "imported": True})
        self._append({"kind": "note", "text": "Operator left the terminal."})
        self.terminal = False
        return len(entries)

    def on_card(self, card: dict) -> None:
        """A card was raised or changed state: every window sees it; a new one goes to the phone."""
        # card_id, not id: each state change is its own history entry with its own id
        self._append({"kind": "card", "card_id": card["id"],
                      **{k: v for k, v in card.items() if k not in ("id", "fingerprint")}})
        if card.get("state") == "pending" and self.notify:
            try:
                self.notify(card)
            except Exception as e:  # a Telegram hiccup never breaks a turn
                _log.warning("conductor.card_notify_failed: %s", type(e).__name__)

    @staticmethod
    def _to_telegram(user: dict) -> bool:
        return user.get("source") == "telegram" or user.get("reply_via") == "telegram"

    def _reply(self, user: dict, text: str) -> None:
        if not self.reply or not self._to_telegram(user):
            return
        if user.get("source") == "autopilot":
            text = f"Autopilot: {text}"  # never mistaken for the operator's own messages
        try:
            self.reply(text)
        except Exception as e:  # a Telegram hiccup never breaks a turn
            _log.warning("conductor.telegram_reply_failed: %s", type(e).__name__)

    def _error(self, user: dict, detail: str, reply: bool = True) -> None:
        self._append({"kind": "status", "state": "error", "msg_id": user["id"], "detail": detail})
        if reply:
            self._reply(user, f"Stopped: {detail}")

    def _append(self, entry: dict) -> dict:
        entry = self.store.append(entry)
        self._publish({"type": "entry", "entry": entry})
        return entry

    def _publish(self, ev: dict) -> None:
        for q in list(self._subs):
            if ev["type"] == "activity" and q.maxsize and q.qsize() >= q.maxsize - max(1, q.maxsize // 10):
                continue  # steps leave room at the top for entries, so they never get a window cut off
            if not q.full():
                q.put_nowait(ev)
            elif ev["type"] not in ("delta", "activity"):  # live-only events may be skipped
                # an entry is never silently lost: a window that can't keep up is dropped and
                # re-reads /history when it reconnects
                _log.warning("conductor.subscriber_dropped")
                self._dropped.add(id(q))
                self.unsubscribe(q)

    def _mark_interrupted(self) -> None:
        last: dict[str, str] = {}
        users: dict[str, dict] = {}
        for e in self.store.history(limit=100000):
            if e.get("kind") == "status":
                last[e["msg_id"]] = e["state"]
            elif e.get("kind") == "user":
                users[e["id"]] = e
        for msg_id, state in last.items():
            if state in ("queued", "running"):
                # not re-run: the turn may already have had side effects
                detail = "the conductor restarted before this finished; send it again if needed"
                self._append({"kind": "status", "state": "interrupted", "msg_id": msg_id, "detail": detail})
                self._reply(users.get(msg_id, {}), f"Interrupted: {detail}")

    async def _worker(self) -> None:
        while True:
            user = await self._pending.get()
            self.busy = True
            try:
                await self._turn(user)
            finally:
                self.busy = False

    async def _turn(self, user: dict) -> None:
        msg_id = user["id"]
        with open(self.lock_path, "w") as lock:
            await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)  # waits out a terminal turn (C5)
            if self.terminal:  # we got the lock but the terminal never said it left: it died
                self.terminal = False
                self._append({"kind": "note", "text": "Terminal session ended without saying goodbye; "
                                                      "its turns aren't in this history."})
            if user.get("source") == "autopilot" and self.autopilot is not None:
                # re-checked now, not only when queued: switched off, quiet hours or a card may have
                # come up while it waited (behind other turns or the terminal's lock)
                why = self.autopilot.runnable(cards_pending=bool(self.cards_pending()))
                if why:
                    self._append({"kind": "status", "state": "skipped", "msg_id": msg_id,
                                  "detail": f"autopilot turn skipped: {why}"})
                    return
            self._activity_msg, self._activity = msg_id, {}  # this turn's steps start empty
            self._append({"kind": "status", "state": "running", "msg_id": msg_id})
            parts: list[str] = []
            prompt = f"[{user.get('source', 'web')}] {user['text']}"
            self._cancelled, self._cancel_via = False, None

            def emit(ev: dict) -> None:
                if ev.get("type") == "delta":
                    parts.append(ev.get("text", ""))
                    self._publish({"type": "delta", "msg_id": msg_id, "text": ev.get("text", "")})
                elif ev.get("type") == "activity":
                    self._on_activity(msg_id, ev)

            try:
                try:
                    sid = await self._run(prompt, self.store.session_id, emit)
                except ResumeFailed:
                    self.store.set_session_id(None)
                    self._append({"kind": "note", "text": "previous conversation could not be resumed; starting fresh"})
                    parts.clear()
                    sid = await self._run(prompt, None, emit)
            except asyncio.CancelledError:
                if not self._cancelled:
                    raise  # the engine itself is stopping
                self._close_activity(msg_id, "failed")
                self._error(user, "cancelled by operator", reply=self._cancel_via != "telegram")
                self._after_turn("cancelled")
                return
            except asyncio.TimeoutError:
                self._close_activity(msg_id, "failed")
                self._error(user, f"timed out after {self.turn_timeout_s:.0f}s; the turn was stopped")
                self._after_turn("timeout")
                return
            except Exception as e:  # the worker must outlive any one turn
                self._close_activity(msg_id, "failed")
                self._error(user, f"{type(e).__name__}: {str(e)[:300]}")
                self._after_turn("error")
                return
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            if sid:
                self.store.set_session_id(sid)
            text = "".join(parts) or "(no reply text)"
            self._close_activity(msg_id, "done")  # a step whose result never came back still ended with the turn
            self._append({"kind": "assistant", "text": text, "reply_to": msg_id})
            self._append({"kind": "status", "state": "done", "msg_id": msg_id})
            self._reply(user, text)
            self._after_turn("done")
