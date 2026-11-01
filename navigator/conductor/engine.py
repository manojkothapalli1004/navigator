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
TurnRunner = Callable[[str, "str | None", Callable[[dict], None]], Awaitable["str | None"]]


class ResumeFailed(Exception):
    """The stored session id could not be resumed (deleted or corrupt transcript)."""


class Engine:
    def __init__(self, store: Store, runner: TurnRunner, lock_path: Path | None = None):
        self.store, self.runner = store, runner
        self.lock_path = Path(lock_path) if lock_path else store.root / "turn.lock"
        self._pending: asyncio.Queue | None = None
        self._subs: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None
        self.busy = False

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

    def submit(self, text: str, source: str) -> dict:
        user = self._append({"kind": "user", "text": text, "source": source})
        ahead = self.queue_len + (1 if self.busy else 0)
        self._append({"kind": "status", "state": "queued", "msg_id": user["id"], "detail": ahead})
        self._pending.put_nowait(user)
        return user

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
                self.unsubscribe(q)

    def _mark_interrupted(self) -> None:
        last: dict[str, str] = {}
        for e in self.store.history(limit=100000):
            if e.get("kind") == "status":
                last[e["msg_id"]] = e["state"]
        for msg_id, state in last.items():
            if state in ("queued", "running"):
                # not re-run: the turn may already have had side effects
                self._append({"kind": "status", "state": "interrupted", "msg_id": msg_id,
                              "detail": "the conductor restarted before this finished; send it again if needed"})

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
            self._append({"kind": "status", "state": "running", "msg_id": msg_id})
            parts: list[str] = []

            def emit(ev: dict) -> None:
                if ev.get("type") == "delta":
                    parts.append(ev.get("text", ""))
                    self._publish({"type": "delta", "msg_id": msg_id, "text": ev.get("text", "")})

            try:
                try:
                    sid = await self.runner(user["text"], self.store.session_id, emit)
                except ResumeFailed:
                    self.store.set_session_id(None)
                    self._append({"kind": "note", "text": "previous conversation could not be resumed; starting fresh"})
                    parts.clear()
                    sid = await self.runner(user["text"], None, emit)
            except Exception as e:  # the worker must outlive any one turn
                self._append({"kind": "status", "state": "error", "msg_id": msg_id,
                              "detail": f"{type(e).__name__}: {str(e)[:300]}"})
                return
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            if sid:
                self.store.set_session_id(sid)
            self._append({"kind": "assistant", "text": "".join(parts) or "(no reply text)", "reply_to": msg_id})
            self._append({"kind": "status", "state": "done", "msg_id": msg_id})
