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
import math
import os
import shutil
import signal
import subprocess
import threading
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
HARD_MAX_RUNNING = 8  # every running helper, approved-card ones included
HELPER_MIN_TIMEOUT_S = 60.0
HELPER_MAX_TIMEOUT_S = 1800.0
LONG_S = 600.0  # a helper with a longer timeout is "long": only one runs at a time
RESULT_CHARS = 12000
TRUNCATED = "\n[… report cut at {n} characters]"
SCRATCH = "/Users/Manoj/calwbot/tmp_claude"
KEEP_FINISHED = 50  # finished records kept in the registry
SNAPSHOT_FINISHED = 10  # finished helpers shown to the conversation and the screens
LAST_TITLES = 50  # step titles remembered per helper (the snapshot shows the last two)
ACTIVE = ("running", "queued")
QUEUE_MAX_S = 2 * 3600.0  # a queued helper older than this (wall clock) is ended with a "delegate again" report
CANCEL_GRACE_S = 15.0  # how long a cancelled/timed-out runner gets to unwind before the helper is ended anyway
TICK_S = 30.0  # the engine's watchdog period
HELPER_PROMPT = (
    "You are a helper the operator's conversation started. Do only the task below, in a worktree "
    "under /Users/Manoj/calwbot/tmp_claude when you must write files, never in a live checkout. "
    "Finish with a short plain report of what you found or did and anything that needs a decision; "
    "that report is all the conversation will see. "
    "A shell command that may run over 9 minutes (a full test suite): start it with nohup, as its own process "
    "group under a perl alarm limit, and record its pid so it can be stopped if you are cancelled, e.g. "
    "`nohup perl -e 'setpgrp; alarm shift; exec @ARGV' 1500 sh -c \"CMD; touch DONE_MARKER\" > LOG 2>&1 & "
    "echo $! > \"$NAV_HELPER_PIDDIR/NAME.pid\"` (this Mac has no coreutils time-limit command, so use that "
    "perl form; NAV_HELPER_PIDDIR is set for you; do the same for any server or loop you leave running), "
    "writing to a log file in the scratch folder and a DONE marker when it ends, then wait with ONE Bash call "
    "that loops `until [ -f DONE_MARKER ]; do sleep 15; done` for at most 9 minutes, and repeat that single "
    "wait call; never poll with many separate model turns. "
    "Keep the closing report under 10000 characters.")
# appended for a normal helper when CONDUCTOR_HELPER_ORC is on (never for an approved-card helper)
HELPER_ORC_PROMPT = (
    "Read-only ORC project-memory tools are available (mcp__orchestrator__get_canonical_memory with "
    "level='auto' or since=<ISO date>; mcp__orchestrator__search). Use them only when the task needs "
    "project state; they cost tokens. If they are absent, continue without them. Never call ORC write tools.")


def clamp_timeout(v) -> float | None:
    """None when not given or not a finite number, else seconds within the helper bounds."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return min(max(f, HELPER_MIN_TIMEOUT_S), HELPER_MAX_TIMEOUT_S)


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


def helper_options(hook, exact: dict | None = None, helper_id: str | None = None,
                   pid_root: Path | None = None) -> ClaudeAgentOptions:
    from navigator.conductor import runner  # runner imports engine; a module-level import would cycle
    # Kill switch CONDUCTOR_HELPER_ORC, read at call time so a live engine follows a change without a
    # restart: unset or any value other than the exact string "0" = on; "0" = today's helper, byte for
    # byte (no MCP server, HELPER_TOOLS only, nothing disallowed, HELPER_PROMPT alone). An approved-card
    # helper never gets ORC: its one approved call is all it may make.
    orc = exact is None and os.environ.get("CONDUCTOR_HELPER_ORC", "1") != "0"
    tools = [exact["tool"]] if exact else list(HELPER_TOOLS)
    if orc:  # explicit, as the main turn lists them: --tools is the base set, and these names are in it
        tools += [t for t in runner.MAIN_TOOLS if t.startswith("mcp__orchestrator__")]
    env: dict[str, str] = {}
    if helper_id and pid_root is not None and exact is None:
        d = Path(pid_root) / helper_id
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        env["NAV_HELPER_PIDDIR"] = str(d)
    # never `resume`: a helper must not pull the shared conversation into its own session
    return ClaudeAgentOptions(
        model=HELPER_MODEL, cwd=exact["cwd"] if exact else runner.CWD,
        setting_sources=["user", "project", "local"], settings=runner.HELPER_LEAN_SETTINGS,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": HELPER_PROMPT + " " + HELPER_ORC_PROMPT if orc else HELPER_PROMPT},
        permission_mode="auto", tools=tools,
        # strict_mcp_config: no server from the operator's settings files loads (`tools` does not govern
        # MCP tools and the gate knows nothing about them, so alpaca's order tools would be one call
        # away). The ONLY server a normal helper gets is the orchestrator's, named here exactly as the
        # main turn names it, so a helper can read canonical project memory instead of re-exploring;
        # its write tools are blocked by name through disallowed_tools, the one option that does govern
        # MCP tools. Never the conductor server (a helper must not delegate). A card helper gets none.
        strict_mcp_config=True, mcp_servers={"orchestrator": runner.ORC_MCP} if orc else {},
        disallowed_tools=list(runner.ORC_WRITE_TOOLS) if orc else [],
        max_turns=HELPER_MAX_TURNS, include_partial_messages=True,
        # the fence runs first: a call it refuses must not consume an approved card
        hooks={"PreToolUse": [HookMatcher(hooks=[_only(exact) if exact else scratch_only_hook, hook])]},
        can_use_tool=gate.can_use_tool,
        # where the helper records the pids of background jobs it starts, so reap() can stop them if the helper
        # is cancelled/times out/fails; never for an approved-card helper: its options stay as they were
        env=env)


MAX_REAP = 50
TERM_WAIT_S = 5.0


def _pid_info(pid: int) -> tuple[int, str] | None:
    """(uid, state) of a live pid, None when it is gone. A zombie counts as gone to the caller (state Z)."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "uid=,stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.split()
        return (int(out[0]), out[1]) if len(out) >= 2 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _protected() -> set[int]:
    """The conductor's own pid and every ancestor: never signalled."""
    skip, p = {os.getpid()}, os.getpid()
    for _ in range(64):
        try:
            out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(p)],
                                 capture_output=True, text=True, timeout=10).stdout.split()
            p = int(out[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            break
        if p <= 1 or p in skip:
            break
        skip.add(p)
    skip.add(os.getppid())
    return skip


def _mine(pid: int) -> bool:
    info = _pid_info(pid)
    return info is not None and info[0] == os.getuid() and not info[1].startswith("Z")


def _signal(pid: int, sig: int, group: bool) -> None:
    try:
        os.killpg(pid, sig) if group else os.kill(pid, sig)
    except OSError:
        pass


def reap(pid_dir: Path) -> int:
    """Kill what a cancelled/timed-out/failed helper left running and recorded in its pid dir (cooperative:
    the helper writes `<name>.pid` files, see HELPER_PROMPT). Same uid only; the process group when the pid
    leads one (and it is not ours), else the pid alone. SIGTERM, TERM_WAIT_S, SIGKILL, then the dir is
    removed. Never the conductor or its ancestors. Returns how many entries were signalled."""
    pid_dir = Path(pid_dir)
    pids: list[int] = []
    try:
        for f in sorted(pid_dir.glob("*.pid"))[:MAX_REAP]:
            try:
                n = int(f.read_text().strip().split()[0])
            except (OSError, ValueError, IndexError):
                continue
            if n > 1 and n not in pids:
                pids.append(n)
    except OSError:
        pass
    skip = _protected() if pids else set()
    own_pg = os.getpgrp()
    targets: dict[int, bool] = {}  # pid -> signal its whole group
    for pid in pids:
        if pid in skip or not _mine(pid):
            continue
        try:
            leader = os.getpgid(pid) == pid and pid != own_pg
        except OSError:
            continue
        targets[pid] = leader
    for pid, grp in targets.items():
        _signal(pid, signal.SIGTERM, grp)
    if targets:
        end = time.monotonic() + TERM_WAIT_S
        while time.monotonic() < end and any(_mine(p) for p in targets):
            time.sleep(0.1)
        for pid, grp in targets.items():
            if _mine(pid):  # re-checked now: a recycled pid of another user is never hit
                _signal(pid, signal.SIGKILL, grp)
        _log.warning("conductor.helper_reaped: %d", len(targets))
    shutil.rmtree(pid_dir, ignore_errors=True)
    return len(targets)


async def sdk_helper(task: str, helper_id: str, emit, *, hook, exact: dict | None = None,
                     pid_root: Path | None = None) -> str:
    """One fresh SDK session for `task`; returns the text of its LAST assistant text block (the
    closing report), so that is what survives the result cap. `hook` is the card hook the main turn
    uses, so a gated action here raises a card exactly as before; `exact` (an approved card) narrows
    the session to that one call."""
    parts: list[str] = []
    async with ClaudeSDKClient(options=helper_options(hook, exact, helper_id, pid_root)) as client:
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
                 max_running: int = MAX_RUNNING, mono=time.monotonic):
        self.path = Path(path)
        self.runner, self.on_event, self.on_finish = runner, on_event, on_finish
        self.clock, self.max_running, self.mono = clock, max_running, mono
        self._mono_launch: dict[str, float] = {}  # in memory only: run time is monotonic (no Mac sleep)
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
        """Running helpers that count against max_running (approved-card helpers take no slot)."""
        return sum(1 for r in self._records if r.get("state") == "running" and not r.get("exact"))

    def _running_all(self) -> int:
        return sum(1 for r in self._records if r.get("state") == "running")

    def _may_launch(self, rec: dict) -> bool:
        if self._running_all() >= HARD_MAX_RUNNING:
            return False
        if (rec.get("timeout_s") or 0) > LONG_S and self._running_long() >= 1:
            return False
        return bool(rec.get("exact")) or self._running() < self.max_running

    def _running_long(self) -> int:
        return sum(1 for r in self._records if r.get("state") == "running" and (r.get("timeout_s") or 0) > LONG_S)

    # ---- lifecycle ----

    def start(self, title: str, task: str, *, exact: dict | None = None, via: str | None = None,
              files: list[str] | None = None, timeout_s: float | None = None) -> dict:
        rec = {"id": new_id("helper"), "title": title, "task": task, "state": "queued",
               "started": self.clock(), "ended": None, "result": None, "error": None,
               "reply_via": via if via is not None else self.current_via, "exact": exact,
               "timeout_s": None if exact else clamp_timeout(timeout_s),
               "files": [str(f).strip() for f in files if str(f).strip()] if files else None}
        self._records.append(rec)
        if self._may_launch(rec):
            self._launch(rec)
        try:
            self._save()
        except Exception as e:  # the helper is already running: never report a failed start for it
            _log.warning("conductor.helpers_save_failed: %s", type(e).__name__)
        return self._public(rec)

    def _launch(self, rec: dict) -> None:
        rec["state"] = "running"
        rec["launched"] = self.clock()
        self._mono_launch[rec["id"]] = self.mono()
        task = asyncio.create_task(self._run(rec))

        def closed(_task) -> None:
            # cancelled before its first step, so _run's own handlers never ran: still one end, one report
            if rec["state"] == "running" and not self._stopping:
                self._end(rec, "cancelled")
        task.add_done_callback(closed)
        self._tasks[rec["id"]] = task

    def _start_queued(self) -> None:
        try:
            for rec in self._records:
                if rec.get("state") == "queued" and self._may_launch(rec):
                    self._launch(rec)
            self._save()
        except Exception as e:  # never strand the queue behind a failed save
            _log.warning("conductor.helper_save_failed: %s", type(e).__name__)

    def _end(self, rec: dict, state: str, **fields) -> None:
        """One end, one report: idempotent, and it never raises (save, report and queue are each guarded)."""
        if rec.get("state") not in ACTIVE:
            return
        rec.update(state=state, ended=self.clock(), **fields)
        self._tasks.pop(rec["id"], None)
        self._mono_launch.pop(rec["id"], None)
        try:
            self._save()
        except Exception as e:
            _log.warning("conductor.helper_save_failed: %s", type(e).__name__)
        try:
            self.on_finish(rec)
        except Exception as e:  # the next helper must start even if the engine choked on the report
            _log.warning("conductor.helper_finish_failed: %s", type(e).__name__)
        self._start_queued()
        if state == "done" or state == "cancelled" and not rec.get("launched"):
            shutil.rmtree(self._piddir(rec["id"]), ignore_errors=True)  # success: leave the processes alone
        elif (state in ("cancelled", "failed") and rec.get("launched") and not rec.get("exact")
                and os.environ.get("CONDUCTOR_HELPER_REAP", "1") != "0"):
            hid = rec["id"]

            def work() -> None:
                try:
                    self._reap(hid)
                except Exception as e:
                    _log.warning("conductor.helper_reap_failed: %s", type(e).__name__)
            threading.Thread(target=work, daemon=True).start()

    def _piddir(self, helper_id: str) -> Path:
        return self.path.parent / "helper-pids" / helper_id

    def _reap(self, helper_id: str) -> int:
        return reap(self._piddir(helper_id))

    @staticmethod
    def _inner_done(t: asyncio.Task) -> None:
        if not t.cancelled():
            t.exception()  # an orphaned runner never logs "exception was never retrieved"

    @staticmethod
    async def _stop_inner(t: asyncio.Task) -> None:
        """Cancel the runner and give it CANCEL_GRACE_S to unwind; never wait on it unbounded."""
        if not t.done():
            t.cancel()
            await asyncio.wait({t}, timeout=CANCEL_GRACE_S)
        if not t.done():
            _log.warning("conductor.helper_cancel_slow")

    async def _run(self, rec: dict) -> None:
        hid = rec["id"]

        def emit(ev: dict) -> None:
            if ev.get("type") == "activity" and ev.get("state") == "running" and ev.get("title"):
                self._titles.setdefault(hid, deque(maxlen=LAST_TITLES)).append(ev["title"])
            self.on_event(hid, ev)

        timeout = min(rec.get("timeout_s") or HELPER_TIMEOUT_S, HELPER_MAX_TIMEOUT_S)  # call time: tests patch
        t = asyncio.ensure_future(self.runner(rec["task"], hid, emit, exact=rec.get("exact")))
        t.add_done_callback(self._inner_done)
        state, fields, skip_end = "cancelled", {}, False
        try:
            done, _ = await asyncio.wait({t}, timeout=timeout)
            if not done:
                hint = (f" (delegate again with timeout_s up to {HELPER_MAX_TIMEOUT_S:.0f} if it needs longer)"
                        if timeout < HELPER_MAX_TIMEOUT_S else "")
                state, fields = "failed", {"error": f"timed out after {timeout:.0f}s{hint}"}
                await self._stop_inner(t)
            else:
                text = t.result()
                masked = activity._SECRET.sub("…", text or "")
                if len(masked) > RESULT_CHARS:
                    masked = masked[:RESULT_CHARS] + TRUNCATED.format(n=len(masked))
                state, fields = "done", {"result": masked}
        except asyncio.CancelledError:
            if self._stopping:
                t.cancel()
                skip_end = True  # shutdown already marked it lost; the restart reports it
                raise
            state, fields = "cancelled", {}
            await self._stop_inner(t)
        except Exception as e:
            state, fields = "failed", {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        finally:
            if not skip_end:
                self._end(rec, state, **fields)

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

    def running_work(self) -> list:
        """Every queued/running helper as a WorkItem; files None = undeclared."""
        from .autopilot import WorkItem
        return [WorkItem("helper", r["id"], frozenset(r["files"]) if r.get("files") else None)
                for r in self._records if r.get("state") in ACTIVE]

    def expire_queued(self) -> None:
        """A helper that waited QUEUE_MAX_S (wall clock) behind a long one ends with a delegate-again report."""
        now = self.clock()
        for rec in list(self._records):
            if rec.get("state") == "queued" and now - (rec.get("started") or now) > QUEUE_MAX_S:
                self._end(rec, "failed", error=f"waited {now - rec['started']:.0f}s in the queue behind a "
                                               f"long helper; delegate again")

    def tick(self) -> None:
        """Watchdog (the engine calls it every TICK_S): end a running helper whose own timeout did not
        (monotonic clock: a sleeping Mac does not run the helper), expire stale queued ones, restart the queue."""
        try:
            for rec in list(self._records):
                lm = self._mono_launch.get(rec["id"])
                if rec.get("state") != "running" or rec.get("exact") or lm is None:
                    continue
                timeout = min(rec.get("timeout_s") or HELPER_TIMEOUT_S, HELPER_MAX_TIMEOUT_S)
                if self.mono() - lm > timeout + CANCEL_GRACE_S + TICK_S:
                    task = self._tasks.get(rec["id"])
                    self._end(rec, "failed", error=f"timed out after {timeout:.0f}s")
                    if task is not None:
                        task.cancel()
            self.expire_queued()
            self._start_queued()
        except Exception as e:
            _log.warning("conductor.helper_tick_failed: %s", type(e).__name__)

    def snapshot(self) -> list[dict]:
        active = [r for r in self._records if r.get("state") in ACTIVE]
        finished = [r for r in self._records if r.get("state") not in ACTIVE][-SNAPSHOT_FINISHED:]
        now = self.clock()
        out = []
        for r in active + finished:
            lm = self._mono_launch.get(r["id"])
            if r.get("state") == "running":
                age = self.mono() - lm if lm is not None else now - (r.get("launched") or r.get("started") or now)
            else:
                age = now - (r.get("started") or now)
            queued_s = (r["launched"] - r["started"]) if r.get("launched") and r.get("started") else 0
            out.append({"id": r["id"], "title": r.get("title"), "state": r.get("state"), "age": age,
                        "queued_s": max(queued_s, 0), "ended": r.get("ended"),
                        "last": list(self._titles.get(r["id"], ()))[-2:]})
        return out
