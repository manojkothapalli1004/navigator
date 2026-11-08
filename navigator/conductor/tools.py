"""The conversation's only hands: an in-process MCP server with delegate / helpers / cancel_helper.

The main turn has no file, shell or web tools (runner.MAIN_TOOLS); everything that needs doing
becomes a helper (kind "quick") or a Navigator build (kind "build") through `delegate`, which returns
at once so the turn ends and the next operator message can be answered. Pattern:
runtime/drivers/builder_tools.make_run_tests_server."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time

import httpx

MIN_TASK_CHARS = 40  # "fix it" is not a task a helper can do alone
TOOL_NAMES = ["delegate", "helpers", "cancel_helper"]
# a full JSON schema, which the SDK passes through as is; a {name: type} dict would make every key required
DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {"kind": {"type": "string"}, "title": {"type": "string"}, "task": {"type": "string"},
                   "project": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
                   "spec_path": {"type": "string"}, "timeout_s": {"type": "integer"}},
    "required": ["kind", "title", "task"]}
DELEGATE_DESCRIPTION = (
    "Hand work off. Returns at once; the result arrives later as a [helper …] or [build …] message. Check "
    "helpers first so the same work is never started twice. kind=\"quick\" (default): a helper does a lookup or "
    "check on a complete, self-contained task (what, where, what done looks like, what to report). "
    "kind=\"build\": code that should be merged goes to Navigator's build pipeline; give project (navigator, "
    "act, waker, orchestrator) and a COMPLETE spec, because the builder sees nothing else: repo, falsifiable "
    "problem, exact change, tests that fail first, must-not, rollback. Docs/tests-only builds start without "
    "approval, every other build waits for the operator, and nothing merges without the operator's OK. "
    "List `files` precisely (repo-relative paths the work will touch): a vague or empty list makes autopilot wait. "
    "For a long spec, write it to a .md file under /Users/Manoj/calwbot/tmp_claude/ (or a docs folder) with a "
    "helper, then pass spec_path=<absolute path>; the conductor reads the file itself and files its text as the "
    "build; task is then only a 20-2000 character summary. For long-running quick work (full test suite, "
    "multi-part review) pass timeout_s (60-1800, default 600).")
HELPERS_DESCRIPTION = ("List helpers and builds: running and queued first, then the last finished ones, "
                       "with their latest steps.")
CANCEL_DESCRIPTION = "Stop a running or queued helper by id."
NO_BUILDS = "refused: builds are not configured"
ACT_DOWN = "refused: ACT is not reachable (NAVIGATOR_ACT_BASE_URL)"


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _files(args) -> list[str] | None:
    raw = args.get("files")
    if isinstance(raw, str):  # the model may send "a.py, b.py" or a JSON list in a string
        raw = raw.strip()
        parsed = None
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        raw = parsed if isinstance(parsed, list) else re.split(r"[,\n]", raw)
    if not isinstance(raw, (list, tuple)):
        return None
    return [str(f).strip() for f in raw if str(f).strip()] or None


async def _delegate_build(builds, args, title: str, task: str) -> str:
    from navigator.conductor.builds import BuildRefused
    if builds is None:
        return NO_BUILDS
    project = str(args.get("project") or "").strip()
    if args.get("timeout_s") is not None:
        return "refused: timeout_s is for quick helpers, not builds"
    spec_path = str(args.get("spec_path") or "").strip()
    kw = {"spec_path": spec_path} if spec_path else {}
    try:  # filing does blocking HTTP + git + file reads: off the event loop
        rec = await asyncio.to_thread(builds.file, project, title, task, _files(args) or [],
                                      getattr(builds, "current_via", None), **kw)
    except BuildRefused as e:
        return f"refused: {e}"
    except (httpx.TransportError, ConnectionError, TimeoutError):  # never a quick-helper fallback
        return ACT_DOWN
    except Exception as e:
        return f"refused: build filing failed ({type(e).__name__})"
    head = f"filed build {rec['proposal_id']} ({project}): {rec['title']}"
    if rec.get("spec_chars"):
        head += f" — spec read from file ({rec['spec_chars']} chars)"
    head += " — "
    if rec.get("tier1"):
        return head + "tier 1 (docs/tests only), approved to start; it will not merge without your approval."
    reason = rec.get("approval_reason") or "needs your approval"
    return head + (f"waiting for operator approval: {reason}. An Approve/Decline card is in the chat; "
                   "I'll tell you when it ends.")


async def delegate_text(helpers, args, builds=None) -> str:
    args = args if isinstance(args, dict) else {}
    kind = str(args.get("kind") or "quick").strip().lower()
    if kind not in ("quick", "build"):
        return "refused: kind must be quick or build"
    title = str(args.get("title") or "").strip()
    task = str(args.get("task") or "").strip()
    if kind == "build":
        return await _delegate_build(builds, args, title, task)
    if not title:
        return "refused: give the helper a short title"
    if len(task) < MIN_TASK_CHARS:
        return (f"refused: the task must be self-contained (at least {MIN_TASK_CHARS} characters: what, "
                "where, what done looks like, what to report)")
    if args.get("spec_path"):
        return "refused: spec_path is only for kind=build"
    files = _files(args)
    kw = {"files": files} if files else {}
    note = ""
    if args.get("timeout_s") is not None:
        from navigator.conductor.helpers import (HELPER_MAX_TIMEOUT_S, HELPER_MIN_TIMEOUT_S, LONG_S,
                                                 clamp_timeout)
        raw = args["timeout_s"]
        try:
            val = None if isinstance(raw, bool) else float(raw)
        except (TypeError, ValueError):
            val = None
        if val is None or not math.isfinite(val):
            return "refused: timeout_s must be a number of seconds"
        kw["timeout_s"] = clamp_timeout(val)
        if val > HELPER_MAX_TIMEOUT_S:
            note = f" (timeout capped at {HELPER_MAX_TIMEOUT_S:.0f}s)"
        elif val < HELPER_MIN_TIMEOUT_S:
            note = f" (raised to {HELPER_MIN_TIMEOUT_S:.0f}s)"
    rec = helpers.start(title, task, **kw)
    if rec["state"] == "queued" and (kw.get("timeout_s") or 0) > LONG_S:
        note += " (queued: only one long helper runs at a time; it expires after 2 h in the queue)"
    return f"started {rec['id']} ({rec['state']}): {rec['title']}{note}"


async def helpers_text(helpers, builds=None) -> str:
    lines = []
    for s in helpers.snapshot():
        waited = f" (queued {s['queued_s']:.0f}s)" if s.get("queued_s", 0) >= 1 else ""
        lines.append(f"{s['id']} {s['state']} {s['age']:.0f}s{waited} — {s['title']} | {', '.join(s['last'])}")
    if builds is not None:
        now = time.time()
        recs = builds.snapshot()
        open_ = [r for r in recs if r.get("state") in ("filed", "running", "review")]
        shown = open_ + [r for r in recs if r not in open_][-5:]
        lines += [f"build {r['proposal_id']} {r['state']} {now - (r.get('filed') or now):.0f}s "
                  f"{', '.join(r.get('files') or []) or 'undeclared'} — {r['title']}" for r in shown]
    return "\n".join(lines) or "no helpers"


async def cancel_text(helpers, args) -> str:
    args = args if isinstance(args, dict) else {}
    hid = str(args.get("id") or "").strip()
    return f"cancelled {hid}" if helpers.cancel(hid) else f"not running: {hid}"


def make_conductor_server(helpers, builds=None):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("delegate", DELEGATE_DESCRIPTION, DELEGATE_SCHEMA)
    async def delegate(args):
        return _text(await delegate_text(helpers, args, builds))

    @tool("helpers", HELPERS_DESCRIPTION, {})
    async def list_helpers(args):
        return _text(await helpers_text(helpers, builds))

    @tool("cancel_helper", CANCEL_DESCRIPTION, {"id": str})
    async def cancel_helper(args):
        return _text(await cancel_text(helpers, args))

    return create_sdk_mcp_server("conductor", tools=[delegate, list_helpers, cancel_helper])
