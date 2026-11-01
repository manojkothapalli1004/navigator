"""One conductor turn through the Claude Agent SDK, resuming the shared session."""
from __future__ import annotations

import re
from typing import Callable

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent

from navigator.conductor import gate
from navigator.conductor.engine import ResumeFailed

MODEL = "claude-opus-5-5"
CWD = "/Users/Manoj/calwbot"
_MISSING_SESSION = re.compile(r"no conversation found|session.{0,40}(not found|does not exist)", re.I)


WINDOWS_PROMPT = (
    "You are the operator's one shared conversation, reached from three windows: the ACT website "
    "chat panel, Telegram and the terminal. Each operator message starts with the window it came "
    "from: [web], [telegram] or [terminal]. Replies are usually read on a phone: keep them short "
    "and plain. To offer the operator a page of the website, put it on its own line as "
    "[open:/path], e.g. [open:/navigator/decisions]; pages: /, /navigator, /navigator/decisions, "
    "/navigator/settings, /trader. Gated actions (merge, push, history rewrites, "
    "settings/CLAUDE.md edits, launchd changes, trader paths) are refused here: say what you "
    "would run and ask the operator to approve it.")


def options(session_id: str | None, hook=None) -> ClaudeAgentOptions:
    # the same settings terminal Claude uses in this workspace, plus the hard gate
    return ClaudeAgentOptions(
        model=MODEL, cwd=CWD, setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": WINDOWS_PROMPT},
        permission_mode="auto", resume=session_id, include_partial_messages=True,
        hooks={"PreToolUse": [HookMatcher(hooks=[hook or gate.pre_tool_use_hook])]},
        can_use_tool=gate.can_use_tool)


async def sdk_turn(prompt: str, session_id: str | None, emit: Callable[[dict], None],
                   hook=None) -> str | None:
    sid = None
    seen_text = False
    try:
        async with ClaudeSDKClient(options=options(session_id, hook)) as client:
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
                elif isinstance(m, ResultMessage):
                    sid = m.session_id
                    if m.is_error:
                        raise RuntimeError(m.result or m.subtype or "turn failed")
    except Exception as e:
        if session_id and _MISSING_SESSION.search(str(e)):
            raise ResumeFailed(str(e)) from e
        raise
    return sid
