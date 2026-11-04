"""The conversation's only hands: an in-process MCP server with delegate / helpers / cancel_helper.

The main turn has no file, shell or web tools (runner.MAIN_TOOLS); everything that needs doing
becomes a helper through `delegate`, which returns at once so the turn ends and the next operator
message can be answered. Pattern: runtime/drivers/builder_tools.make_run_tests_server."""
from __future__ import annotations

MIN_TASK_CHARS = 40  # "fix it" is not a task a helper can do alone
TOOL_NAMES = ["delegate", "helpers", "cancel_helper"]
DELEGATE_DESCRIPTION = (
    "Start a helper on a complete, self-contained task (what, where, what done looks like, what to "
    "report). Returns at once; the helper's report arrives later as a [helper …] message. Check "
    "helpers first so the same task is never started twice.")
HELPERS_DESCRIPTION = "List helpers: running and queued first, then the last finished ones, with their latest steps."
CANCEL_DESCRIPTION = "Stop a running or queued helper by id."


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


async def delegate_text(helpers, args) -> str:
    args = args if isinstance(args, dict) else {}
    title = str(args.get("title") or "").strip()
    task = str(args.get("task") or "").strip()
    if not title:
        return "refused: give the helper a short title"
    if len(task) < MIN_TASK_CHARS:
        return (f"refused: the task must be self-contained (at least {MIN_TASK_CHARS} characters: what, "
                "where, what done looks like, what to report)")
    rec = helpers.start(title, task)
    return f"started {rec['id']} ({rec['state']}): {rec['title']}"


async def helpers_text(helpers) -> str:
    lines = [f"{s['id']} {s['state']} {s['age']:.0f}s — {s['title']} | {', '.join(s['last'])}"
             for s in helpers.snapshot()]
    return "\n".join(lines) or "no helpers"


async def cancel_text(helpers, args) -> str:
    args = args if isinstance(args, dict) else {}
    hid = str(args.get("id") or "").strip()
    return f"cancelled {hid}" if helpers.cancel(hid) else f"not running: {hid}"


def make_conductor_server(helpers):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("delegate", DELEGATE_DESCRIPTION, {"title": str, "task": str})
    async def delegate(args):
        return _text(await delegate_text(helpers, args))

    @tool("helpers", HELPERS_DESCRIPTION, {})
    async def list_helpers(args):
        return _text(await helpers_text(helpers))

    @tool("cancel_helper", CANCEL_DESCRIPTION, {"id": str})
    async def cancel_helper(args):
        return _text(await cancel_text(helpers, args))

    return create_sdk_mcp_server("conductor", tools=[delegate, list_helpers, cancel_helper])
