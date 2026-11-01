"""Gated actions: whatever the permission settings allow, these always stop for the operator.

C1 denies them with a reason the model sees; C3 turns the same check into an approval card.
Deliberately conservative (a false stop costs one approval; a missed one can merge or rewrite
history): a command is split on shell separators and each piece is checked."""
from __future__ import annotations

import re
from pathlib import Path

HOME = Path.home()
GATED_DIRS = (HOME / ".claude", HOME / "Library" / "LaunchAgents", Path("/Users/Manoj/calwbot/trader"))
MEMORY_DIR = re.compile(r"^" + re.escape(str(HOME / ".claude" / "projects")) + r"/[^/]+/memory/")
GATED_NAMES = {"settings.json", "settings.local.json", "CLAUDE.md", ".claude.json", ".mcp.json"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
GIT_GATED = {"merge", "push", "pull", "rebase", "cherry-pick", "revert", "am", "filter-branch",
             "filter-repo", "update-ref", "replace"}
LAUNCHCTL_GATED = {"load", "unload", "bootstrap", "bootout", "remove", "enable", "disable"}
NAVIGATOR_GATED = {"merge", "reject", "audited", "approve", "decline", "abandon"}
# any of these in a Bash command means it may write a protected file (reads get stopped too:
# the Read tool is the way to read them)
_PROTECTED_TEXT = re.compile(
    r"CLAUDE\.md|settings(\.local)?\.json|\.claude\.json|\.mcp\.json|\.git/hooks|Library/LaunchAgents"
    r"|\.claude/(settings|hooks|plugins|agents|commands|skills|output-styles)")
_TRADER = re.compile(r"(^|[\s/'\"=:])trader(/|\s|$)")
_SPLIT = re.compile(r"[\s;&|(){}<>`$]+")
_REVISION = re.compile(r"^(HEAD|@)([~^]\d*)+$|^[0-9a-f]{7,40}$")
APPROVAL_HINT = ("needs the operator's approval (approval cards arrive in C3; until then ask the "
                 "operator to run it from the terminal)")


def _git_reason(tokens: list[str], i: int) -> str | None:
    """tokens[i] is a git program; look at its subcommand."""
    i += 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c") else 1
    if i >= len(tokens):
        return None
    sub, rest = tokens[i], tokens[i + 1:i + 8]
    if sub in GIT_GATED:
        return f"git {sub} changes branches or history"
    if sub == "reset" and ({"--hard", "--keep", "--merge"} & set(rest) or any(_REVISION.match(t) for t in rest)):
        return "git reset moves a branch"
    if sub == "commit" and "--amend" in rest:
        return "git commit --amend rewrites history"
    if sub == "branch" and {"-f", "--force", "-D", "-d", "--delete", "-M", "-m"} & set(rest):
        return "git branch moves, renames or deletes a branch"
    if sub in ("checkout", "switch") and {"-B", "-C", "--force-create"} & set(rest):
        return f"git {sub} resets a branch"
    return None


def _bash_reason(command: str) -> str | None:
    # one scan over the whole command, quotes removed: wrappers (nohup, xargs, subshells, if/then,
    # bash -lc '...', echo ... | sh) can't hide a gated word anywhere in it. Conservative on
    # purpose - a false stop costs one approval.
    if _TRADER.search(command):
        return "touches trader/ (money paths)"
    if m := _PROTECTED_TEXT.search(command):
        return f"touches {m.group(0)} (permissions, hooks or services)"
    tokens = [t for t in _SPLIT.split(command.replace('"', " ").replace("'", " ")) if t]
    for i, tok in enumerate(tokens):
        prog = tok.rsplit("/", 1)[-1]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if prog == "git" and (r := _git_reason(tokens, i)):
            return r
        if prog == "launchctl" and nxt in LAUNCHCTL_GATED:
            return f"launchctl {nxt} installs or removes a service"
        if prog == "gh":
            return "gh talks to GitHub (git stays local)"
        if prog == "navigator" and nxt in NAVIGATOR_GATED:
            return f"navigator {nxt} decides a run"
    return None


def _path_reason(raw: str, cwd: Path) -> str | None:
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else Path(cwd) / p).resolve()
    if p.name in GATED_NAMES:
        return f"edits {p.name} (permissions, instructions or tools)"
    if MEMORY_DIR.match(str(p)):
        return None
    if ".claude" in p.parts or ".git" in p.parts and "hooks" in p.parts:
        return "writes Claude or git configuration/hooks"
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
