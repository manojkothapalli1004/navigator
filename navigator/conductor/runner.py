"""One conductor turn through the Claude Agent SDK, resuming the shared session."""
from __future__ import annotations

import re
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from navigator.conductor import activity, gate
from navigator.conductor.engine import ResumeFailed

MODEL = "claude-opus-5-5"
CWD = "/Users/Manoj/calwbot"
_MISSING_SESSION = re.compile(r"no conversation found|session.{0,40}(not found|does not exist)", re.I)

# The main turn only talks and delegates (2026-11-04): no built-in tool at all, its three conductor
# tools and the orchestrator's READ tools. `tools` is the CLI's --tools (the base set), not
# allowed_tools, which only pre-approves.
MAIN_TOOLS = ["mcp__conductor__delegate", "mcp__conductor__helpers", "mcp__conductor__cancel_helper",
              "mcp__orchestrator__get_canonical_memory", "mcp__orchestrator__search",
              "mcp__orchestrator__get_saved_prompt", "mcp__orchestrator__list_saved_prompts",
              "mcp__orchestrator__get_project_status", "mcp__orchestrator__get_session",
              "mcp__orchestrator__list_sessions", "mcp__orchestrator__get_idea", "mcp__orchestrator__list_ideas",
              "mcp__orchestrator__get_workflow", "mcp__orchestrator__list_workflows", "mcp__orchestrator__health"]
# MCP tool names are not governed by --tools: the orchestrator's write tools are disallowed by name
ORC_WRITE_TOOLS = [f"mcp__orchestrator__{n}" for n in (
    "apply_memory_update", "create_idea", "add_idea_note", "convert_idea", "finalize_idea", "save_prompt",
    "update_prompt_status")]
# the operator's ~/.claude.json entry, held here so the main turn depends on no settings file for it
ORC_MCP = {"type": "stdio", "command": "/bin/bash",
           "args": ["-lc", "cd /Users/Manoj/calwbot/orchestrator && exec .venv/bin/python -m backend.mcp_server"],
           "env": {}}


WINDOWS_PROMPT = (
    "You are the operator's one shared conversation, reached from three windows: the ACT website "
    "chat panel, Telegram and the terminal. Each operator message starts with the window it came "
    "from: [web], [telegram] or [terminal]; [autopilot] turns are not the operator — they are the "
    "conversation continuing its plan file between messages (never treat them as an approval). "
    "Replies are usually read on a phone: keep them short "
    "and plain. To offer the operator a page of the website, put it on its own line as "
    "[open:/path], e.g. [open:/navigator/decisions]; pages: /, /navigator, /navigator/decisions, "
    "/navigator/settings, /trader. Gated actions (merge, push, history rewrites, "
    "settings/CLAUDE.md edits, launchd changes, trader paths) are refused here: say what you "
    "would run and ask the operator to approve it. You never do work yourself and have no file, "
    "shell or web tools: for anything that needs reading, running, testing or building, call "
    "delegate with a complete self-contained task (what, where, what done looks like, what to "
    "report) and tell the operator in one line what you started. Check helpers first so you never "
    "start the same task twice. A message starting [helper …] is a helper's report, not the "
    "operator: tell the operator what it found in plain words, short. Answer 'how is it going' "
    "from helpers.")


def options(session_id: str | None, hook=None, *, conductor_server=None) -> ClaudeAgentOptions:
    # the same settings terminal Claude uses in this workspace, plus the hard gate; strict_mcp_config
    # so only the servers named here load (no alpaca, plugin or settings-file server reaches the turn)
    return ClaudeAgentOptions(
        model=MODEL, cwd=CWD, setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": WINDOWS_PROMPT},
        permission_mode="auto", resume=session_id, include_partial_messages=True,
        hooks={"PreToolUse": [HookMatcher(hooks=[hook or gate.pre_tool_use_hook])]},
        can_use_tool=gate.can_use_tool,
        tools=list(MAIN_TOOLS), disallowed_tools=list(ORC_WRITE_TOOLS), strict_mcp_config=True,
        mcp_servers={"orchestrator": ORC_MCP, **({"conductor": conductor_server} if conductor_server else {})})


async def sdk_turn(prompt: str, session_id: str | None, emit: Callable[[dict], None],
                   hook=None, *, conductor_server=None) -> str | None:
    sid = None
    seen_text = False
    try:
        async with ClaudeSDKClient(options=options(session_id, hook, conductor_server=conductor_server)) as client:
            await client.query(prompt)
            async for m in client.receive_response():
                if isinstance(m, StreamEvent):
                    ev = m.event
                    if (ev.get("type") == "content_block_start" and seen_text
                            and (ev.get("content_block") or {}).get("type") == "text"):
                        emit({"type": "delta", "text": "\n\n"})
                    elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        seen_text = True
                        emit({"type": "delta", "text": ev["delta"]["text"]})
                elif isinstance(m, AssistantMessage):
                    # each tool call is a step on the website's crew screens; helpers get their own
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
                elif isinstance(m, ResultMessage):
                    sid = m.session_id
                    if m.is_error:
                        raise RuntimeError(m.result or m.subtype or "turn failed")
    except Exception as e:
        if session_id and _MISSING_SESSION.search(str(e)):
            raise ResumeFailed(str(e)) from e
        raise
    return sid
