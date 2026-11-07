"""What the conductor is doing, step by step, for the website's crew screens (2026-11-04).

Each tool call in a turn becomes one short plain line ("Reading bot.py", "Searching the web: …").
Titles never include a shell command or a tool's raw input — a command can carry a token — only
the call's own description, a file's name, a search query or a site's host."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_TITLE = 100
# The one shared secret mask (titles, helper reports, build specs, build summaries): real token
# shapes, PEM headers, and long hex/base64 only right after a key word. It deliberately leaves
# alone test names, repo paths, git shas and words that merely contain a prefix (task-queue-runner).
_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[a-z]-[A-Za-z0-9-]{8,}|\d{6,}:[A-Za-z0-9_-]{25,}|AKIA[0-9A-Z]{12,}|[sr]k_(?:live|test)_[A-Za-z0-9]{8,})"
    # a bare 32+ char run (start/space/quote on both sides, no "/" so never a path) holding an upper,
    # a lower and a digit; a "word-" / "word_" prefix (proposal-01M6…) keeps ids readable
    r"|(?<![^\s\"'])(?![a-z]{3,}[-_])(?=[A-Za-z0-9+=_-]{32,}(?![^\s\"']))"
    r"(?=[A-Za-z0-9+=_-]*\d)(?=[A-Za-z0-9+=_-]*[A-Z])(?=[A-Za-z0-9+=_-]*[a-z])[A-Za-z0-9+=_-]+"
    r"|-----BEGIN [A-Z ]+-----"
    r"|(?i:(?:authorization[ \t]*[=:][ \t]*)?(?:[a-z_-]*(?:key|token|secret|password|passwd|authorization)|bearer|basic)[\"']?[ \t]*[=:]?[ \t]*[\"']?)"
    r"(?:[A-Fa-f0-9]{32,}|(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])[A-Za-z0-9+/]{32,}={0,2})"
    r"(?![A-Za-z0-9+/_=-])[\"']?")
MAX_ITEMS = 200  # steps kept per turn for a window that opens mid-turn

_FILE_VERBS = {"Read": "Reading", "Edit": "Editing", "MultiEdit": "Editing", "Write": "Writing",
               "NotebookEdit": "Editing", "NotebookRead": "Reading"}
_HELPERS = {"Agent", "Task"}


def is_helper(tool: str) -> bool:
    """A helper (subagent) the conversation started: it gets its own screen."""
    return tool in _HELPERS


def _one_line(text: str) -> str:
    t = _SECRET.sub("…", " ".join(str(text).split()))
    return t if len(t) <= MAX_TITLE else t[: MAX_TITLE - 1].rstrip() + "…"


def _name(path: object) -> str:
    return str(path).rstrip("/").rsplit("/", 1)[-1] if path else ""


def describe(tool: str, data: object) -> str:
    """Never raises: a title must not be able to end the turn it describes."""
    try:
        return _describe(str(tool), data)
    except Exception:
        return _one_line(tool)


def _describe(tool: str, data: object) -> str:
    args = data if isinstance(data, dict) else {}
    if tool == "Bash":
        title = str(args.get("description") or "").strip()
        return _one_line(title) if title else "Running a command"
    if tool in _FILE_VERBS:
        name = _name(args.get("file_path") or args.get("notebook_path"))
        return _one_line(f"{_FILE_VERBS[tool]} {name}") if name else f"{_FILE_VERBS[tool]} a file"
    if tool in ("Grep", "Glob"):
        return "Searching the code"
    if tool == "WebSearch":
        q = str(args.get("query") or "").strip()
        return _one_line(f"Searching the web: {q}") if q else "Searching the web"
    if tool == "WebFetch":
        host = urlsplit(str(args.get("url") or "")).hostname
        return _one_line(f"Reading {host}") if host else "Reading a web page"
    if tool in _HELPERS:
        return _one_line(str(args.get("description") or "").strip() or "A helper is working")
    if tool == "Skill":
        return _one_line(f"Using skill {args.get('skill') or ''}".strip())
    if tool == "TodoWrite":
        return "Updating the to-do list"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else "tool"
        action = " ".join(parts[2:]).replace("_", " ") if len(parts) > 2 else "call"
        return _one_line(f"{server}: {action}")
    return _one_line(tool)
