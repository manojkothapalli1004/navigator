"""Helpers: the work a conversation turn used to do itself, run beside it (2026-11-04).

One operator message once became a 26-minute turn (build, commit, audit) while four later
messages sat queued. The main turn now only talks and delegates: each delegated task is its own
short-lived SDK session run here, on a cheaper model, with a write fence that keeps it in the
scratch folder, and its closing report comes back to the conversation as an internal "helper"
message. The registry (helpers.json) survives a restart so lost helpers can be reported."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from navigator.conductor import activity, gate
from navigator.conductor.cards import fingerprint
from navigator.ids import new_id

_log = logging.getLogger(__name__)

HELPER_TOOLS = ["Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "Edit", "Write"]
HELPER_MODEL = "claude-sonnet-5-5"
HELPER_MAX_TURNS = 40
HELPER_TIMEOUT_S = 600.0
MAX_RUNNING = 3
RESULT_CHARS = 4000
SCRATCH = "/Users/Manoj/calwbot/tmp_claude"
KEEP_FINISHED = 50  # finished records kept in the registry
SNAPSHOT_FINISHED = 10  # finished helpers shown to the conversation and the screens
LAST_TITLES = 50  # step titles remembered per helper (the snapshot shows the last two)
ACTIVE = ("running", "queued")
HELPER_PROMPT = (
    "You are a helper the operator's conversation started. Do only the task below, in a worktree "
    "under /Users/Manoj/calwbot/tmp_claude when you must write files, never in a live checkout. "
    "Finish with a short plain report of what you found or did and anything that needs a decision; "
    "that report is all the conversation will see.")


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


async def scratch_only_hook(input_data: dict, tool_use_id, context) -> dict:
    """Edit/Write/MultiEdit/NotebookEdit only under SCRATCH (resolved, so no ../ escape). Bash is
    deliberately not fenced: an allowlist would cripple testing; the gate's Bash rules are the floor."""
    key = gate.WRITE_TOOLS.get(input_data.get("tool_name", ""))
    raw = (input_data.get("tool_input") or {}).get(key) if key else None
    if not raw:
        return {}
    cwd = Path(input_data.get("cwd") or "/Users/Manoj/calwbot")
    p = Path(str(raw)).expanduser()
    p = (p if p.is_absolute() else cwd / p).resolve()
    if Path(SCRATCH).resolve() in p.parents:
        return {}
    return _deny(f"helpers write only inside a worktree under {SCRATCH}; {p} is outside it")


def _only(exact: dict):
    """The fence of an approved-card helper: the one approved call (same fingerprint as its card:
    tool, input minus a Bash label, folder) passes; anything else is denied."""
    want = fingerprint(exact["tool"], exact["input"], exact["cwd"])

    async def hook(input_data: dict, tool_use_id, context) -> dict:
        got = fingerprint(input_data.get("tool_name", ""), input_data.get("tool_input") or {},
                          str(input_data.get("cwd") or "/Users/Manoj/calwbot"))
        if got == want:
            return {}
        return _deny("this helper may only issue the one call the operator approved, unchanged, "
                     "in the approved folder")
    return hook


def helper_options(hook, exact: dict | None = None) -> ClaudeAgentOptions:
    from navigator.conductor import runner  # runner imports engine; a module-level import would cycle
    # never `resume`: a helper must not pull the shared conversation into its own session
    return ClaudeAgentOptions(
        model=HELPER_MODEL, cwd=exact["cwd"] if exact else runner.CWD,
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": HELPER_PROMPT},
        permission_mode="auto", tools=[exact["tool"]] if exact else list(HELPER_TOOLS),
        max_turns=HELPER_MAX_TURNS, include_partial_messages=True,
        hooks={"PreToolUse": [HookMatcher(hooks=[hook, _only(exact) if exact else scratch_only_hook])]},
        can_use_tool=gate.can_use_tool)


async def sdk_helper(task: str, helper_id: str, emit, *, hook, exact: dict | None = None) -> str:
    """One fresh SDK session for `task`; returns the text of its LAST assistant text block (the
    closing report), so that is what survives the result cap. `hook` is the card hook the main turn
    uses, so a gated action here raises a card exactly as before; `exact` (an approved card) narrows
    the session to that one call."""
    parts: list[str] = []
    async with ClaudeSDKClient(options=helper_options(hook, exact)) as client:
        await client.query(task)
        async for m in client.receive_response():
            if isinstance(m, StreamEvent):
                ev = m.event
                if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "text":
                    parts = []
                elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    parts.append(ev["delta"]["text"])
            elif isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        emit({"type": "activity", "id": b.id, "tool": b.name,
                              "title": activity.describe(b.name, b.input), "state": "running",
                              "parent": m.parent_tool_use_id, "helper": activity.is_helper(b.name)})
            elif isinstance(m, UserMessage) and isinstance(m.content, list):
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        emit({"type": "activity", "id": b.tool_use_id,
                              "state": "failed" if b.is_error else "done"})
            elif isinstance(m, ResultMessage) and m.is_error:
                raise RuntimeError(m.result or m.subtype or "helper failed")
    return "".join(parts)


class Helpers:
    """The registry and scheduler of helpers. `runner(task, helper_id, emit, exact=...)` does the
    work (sdk_helper in production, a fake in tests); `on_event(helper_id, ev)` gets its steps,
    `on_finish(record)` its end, whatever the end was."""

    def __init__(self, path: Path, runner, on_event, on_finish, *, clock=time.time,
                 max_running: int = MAX_RUNNING):
        self.path = Path(path)
        self.runner, self.on_event, self.on_finish = runner, on_event, on_finish
        self.clock, self.max_running = clock, max_running
        self.current_via: str | None = None  # set by the engine at each turn start
        self._records: list[dict] = self._load()
        # still running/queued in the file when we started: a previous process died with them
        self._stale = [r for r in self._records if r.get("state") in ACTIVE]
        self._tasks: dict[str, asyncio.Task] = {}
        self._titles: dict[str, deque] = {}
        self._stopping = False

    # ---- registry ----

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            recs = data.get("helpers") if isinstance(data, dict) else None
            return [r for r in recs if isinstance(r, dict) and r.get("id")] if isinstance(recs, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        active = [r for r in self._records if r.get("state") in ACTIVE]
        finished = [r for r in self._records if r.get("state") not in ACTIVE][-KEEP_FINISHED:]
        keep = {id(r) for r in active + finished}
        self._records = [r for r in self._records if id(r) in keep]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"helpers": self._records}))
        os.replace(tmp, self.path)

    def _find(self, helper_id: str) -> dict | None:
        return next((r for r in self._records if r["id"] == helper_id), None)

    def get(self, helper_id: str) -> dict | None:
        rec = self._find(helper_id)
        return dict(rec) if rec else None

    @staticmethod
    def _public(rec: dict) -> dict:
        return {k: rec.get(k) for k in ("id", "title", "state", "started")}

    def _running(self) -> int:
        return sum(1 for r in self._records if r.get("state") == "running")

    # ---- lifecycle ----

    def start(self, title: str, task: str, *, exact: dict | None = None, via: str | None = None) -> dict:
        rec = {"id": new_id("helper"), "title": title, "task": task, "state": "queued",
               "started": self.clock(), "ended": None, "result": None, "error": None,
               "reply_via": via if via is not None else self.current_via, "exact": exact}
        self._records.append(rec)
        if self._running() < self.max_running:
            self._launch(rec)
        self._save()
        return self._public(rec)

    def _launch(self, rec: dict) -> None:
        rec["state"] = "running"
        task = asyncio.create_task(self._run(rec))

        def closed(_task) -> None:
            # cancelled before its first step, so _run's own handlers never ran: still one end, one report
            if rec["state"] == "running" and not self._stopping:
                self._end(rec, "cancelled")
        task.add_done_callback(closed)
        self._tasks[rec["id"]] = task

    def _start_queued(self) -> None:
        for rec in self._records:
            if self._running() >= self.max_running:
                break
            if rec.get("state") == "queued":
                self._launch(rec)
        self._save()

    def _end(self, rec: dict, state: str, **fields) -> None:
        rec.update(state=state, ended=self.clock(), **fields)
        self._tasks.pop(rec["id"], None)
        self._save()
        try:
            self.on_finish(rec)
        except Exception as e:  # the next helper must start even if the engine choked on the report
            _log.warning("conductor.helper_finish_failed: %s", type(e).__name__)
        self._start_queued()

    async def _run(self, rec: dict) -> None:
        hid = rec["id"]

        def emit(ev: dict) -> None:
            if ev.get("type") == "activity" and ev.get("state") == "running" and ev.get("title"):
                self._titles.setdefault(hid, deque(maxlen=LAST_TITLES)).append(ev["title"])
            self.on_event(hid, ev)

        timeout = HELPER_TIMEOUT_S  # read at call time: tests patch it
        try:
            text = await asyncio.wait_for(self.runner(rec["task"], hid, emit, exact=rec.get("exact")), timeout)
        except asyncio.TimeoutError:
            self._end(rec, "failed", error=f"timed out after {timeout:.0f}s")
        except asyncio.CancelledError:
            if self._stopping:
                raise  # shutdown already marked it lost; the restart reports it
            self._end(rec, "cancelled")
        except Exception as e:
            self._end(rec, "failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        else:
            self._end(rec, "done", result=activity._SECRET.sub("…", text or "")[:RESULT_CHARS])

    def cancel(self, helper_id: str) -> bool:
        rec = self._find(helper_id)
        if rec is None:
            return False
        if rec["state"] == "running" and helper_id in self._tasks:
            self._tasks[helper_id].cancel()
            return True
        if rec["state"] == "queued":
            self._end(rec, "cancelled")
            return True
        return False

    def mark_lost(self) -> list[dict]:
        """Records a previous process left running/queued: lost, and reported once by the engine."""
        lost, self._stale = self._stale, []
        for rec in lost:
            rec.update(state="lost", ended=self.clock())
        if lost:
            self._save()
        return [dict(r) for r in lost]

    def shutdown(self) -> None:
        """The engine is stopping: every live helper is lost. Nothing is reported now (the engine
        is dead) and the file keeps saying running/queued so mark_lost reports them on restart."""
        self._stopping = True
        for rec in self._records:
            if rec.get("state") in ACTIVE:
                rec.update(state="lost", ended=self.clock())
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def snapshot(self) -> list[dict]:
        active = [r for r in self._records if r.get("state") in ACTIVE]
        finished = [r for r in self._records if r.get("state") not in ACTIVE][-SNAPSHOT_FINISHED:]
        now = self.clock()
        return [{"id": r["id"], "title": r.get("title"), "state": r.get("state"),
                 "age": now - (r.get("started") or now), "ended": r.get("ended"),
                 "last": list(self._titles.get(r["id"], ()))[-2:]} for r in active + finished]
