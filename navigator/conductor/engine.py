"""One conversation, one turn at a time. Every window submits into the same FIFO; one worker
runs the turns against the persisted Agent SDK session and publishes events to subscribers."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from pathlib import Path
from typing import Awaitable, Callable

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
                 reply: Callable[[str], None] | None = None):
        self.store, self.runner, self.turn_timeout_s = store, runner, turn_timeout_s
        self.notify = notify  # tells the operator's phone about a new approval card (C3)
        self.reply = reply  # sends a Telegram-originated turn's outcome back to Telegram (C4)
        self._dropped: set[int] = set()
        self.lock_path = Path(lock_path) if lock_path else store.root / "turn.lock"
        self._pending: asyncio.Queue | None = None
        self._subs: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._cancelled = False
        self.busy = False
        self.terminal = False  # the operator joined from the terminal (C5)

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

    def cancel(self) -> bool:
        """Stop the running turn (the operator's Stop button); False when nothing runs."""
        if self._turn_task is None or self._turn_task.done():
            return False
        self._cancelled = True
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
        # "approval" messages come only from the conductor itself, after the operator approves a card
        if source not in SOURCES and not (internal and source == "approval"):
            raise ValueError(f"unknown source {source!r}")
        user = self._append({"kind": "user", "text": text, "source": source,
                             **({"reply_via": reply_via} if reply_via else {})})
        ahead = self.queue_len + (1 if self.busy else 0)
        detail = TERMINAL_WAIT if self.terminal else ahead
        self._append({"kind": "status", "state": "queued", "msg_id": user["id"], "detail": detail})
        if self.terminal:
            self._reply(user, "You're in the terminal — I'll answer this when you leave it.")
        self._pending.put_nowait(user)
        return user

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
        try:
            self.reply(text)
        except Exception as e:  # a Telegram hiccup never breaks a turn
            _log.warning("conductor.telegram_reply_failed: %s", type(e).__name__)

    def _error(self, user: dict, detail: str) -> None:
        self._append({"kind": "status", "state": "error", "msg_id": user["id"], "detail": detail})
        self._reply(user, f"Stopped: {detail}")

    def _append(self, entry: dict) -> dict:
        entry = self.store.append(entry)
        self._publish({"type": "entry", "entry": entry})
        return entry

    def _publish(self, ev: dict) -> None:
        for q in list(self._subs):
            if not q.full():
                q.put_nowait(ev)
            elif ev["type"] != "delta":
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
            self._append({"kind": "status", "state": "running", "msg_id": msg_id})
            parts: list[str] = []
            prompt = f"[{user.get('source', 'web')}] {user['text']}"
            self._cancelled = False

            def emit(ev: dict) -> None:
                if ev.get("type") == "delta":
                    parts.append(ev.get("text", ""))
                    self._publish({"type": "delta", "msg_id": msg_id, "text": ev.get("text", "")})

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
                self._error(user, "cancelled by operator")
                return
            except asyncio.TimeoutError:
                self._error(user, f"timed out after {self.turn_timeout_s:.0f}s; the turn was stopped")
                return
            except Exception as e:  # the worker must outlive any one turn
                self._error(user, f"{type(e).__name__}: {str(e)[:300]}")
                return
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            if sid:
                self.store.set_session_id(sid)
            text = "".join(parts) or "(no reply text)"
            self._append({"kind": "assistant", "text": text, "reply_to": msg_id})
            self._append({"kind": "status", "state": "done", "msg_id": msg_id})
            self._reply(user, text)
