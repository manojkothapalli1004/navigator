"""Gated actions: whatever the permission settings allow, these always stop for the operator.

C1 denies them with a reason the model sees; C3 turns the same check into an approval card.
Deliberately conservative (a false stop costs one approval; a missed one can merge or rewrite
history): a command is split on shell separators and each piece is checked."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

HOME = Path.home()
GATED_DIRS = (HOME / ".claude", HOME / "Library" / "LaunchAgents", Path("/Users/Manoj/calwbot/trader"))
MEMORY_DIR = re.compile(r"^" + re.escape(str(HOME / ".claude" / "projects")) + r"/[^/]+/memory/")
GATED_NAMES = {"settings.json", "settings.local.json", "CLAUDE.md"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
GIT_GATED = {"merge", "push", "rebase", "filter-branch", "filter-repo", "update-ref"}
LAUNCHCTL_GATED = {"load", "unload", "bootstrap", "bootout", "remove", "enable", "disable"}
_SEPARATORS = re.compile(r"&&|\|\||[;|\n`]|\$\(")
_TRADER = re.compile(r"(^|[\s/'\"=:])trader(/|\s|$)")
APPROVAL_HINT = ("needs the operator's approval (approval cards arrive in C3; until then ask the "
                 "operator to run it from the terminal)")


def _git_reason(tokens: list[str]) -> str | None:
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c") else 1
    if i >= len(tokens):
        return None
    sub, rest = tokens[i], tokens[i + 1:]
    if sub in GIT_GATED or (sub == "reset" and "--hard" in rest) or (sub == "commit" and "--amend" in rest):
        return f"git {sub} changes history or branches"
    return None


def _bash_reason(command: str) -> str | None:
    if _TRADER.search(command):
        return "touches trader/ (money paths)"
    if "Library/LaunchAgents" in command:
        return "touches ~/Library/LaunchAgents"
    flat = command.replace('"', " ").replace("'", " ")
    for piece in _SEPARATORS.split(flat):
        try:
            tokens = shlex.split(piece)
        except ValueError:
            tokens = piece.split()
        while tokens and (re.match(r"^\w+=", tokens[0]) or tokens[0] in ("sudo", "env", "exec", "command")):
            tokens = tokens[1:]
        while tokens and tokens[0] in ("bash", "sh", "zsh") and len(tokens) > 1 and tokens[1] == "-c":
            tokens = tokens[2:]
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        if prog == "git" and (r := _git_reason(tokens)):
            return r
        if prog == "launchctl" and len(tokens) > 1 and tokens[1] in LAUNCHCTL_GATED:
            return f"launchctl {tokens[1]} installs or removes a service"
    return None


def _path_reason(raw: str, cwd: Path) -> str | None:
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else Path(cwd) / p).resolve()
    if p.name in GATED_NAMES:
        return f"edits {p.name} (permissions/instructions)"
    if MEMORY_DIR.match(str(p)):
        return None
    for d in GATED_DIRS:
        if p == d or d in p.parents:
            return f"writes under {d}"
    return None


def gated_reason(tool_name: str, tool_input: dict, cwd: Path) -> str | None:
    """Why this tool call must stop for the operator, or None."""
    if tool_name == "Bash":
        return _bash_reason(str(tool_input.get("command", "")))
    key = WRITE_TOOLS.get(tool_name)
    if key and tool_input.get(key):
        return _path_reason(str(tool_input[key]), cwd)
    return None


async def pre_tool_use_hook(input_data: dict, tool_use_id, context) -> dict:
    reason = gated_reason(input_data.get("tool_name", ""), input_data.get("tool_input") or {},
                          Path(input_data.get("cwd") or "/Users/Manoj/calwbot"))
    if reason is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"{reason} — {APPROVAL_HINT}"}}


async def can_use_tool(tool_name: str, input_data: dict, context):
    from claude_agent_sdk import PermissionResultDeny
    return PermissionResultDeny(message="this needs a human OK and nobody can answer a prompt in this "
                                        "window; tell the operator what you wanted to run and why")
