"""Gated actions: whatever the permission settings allow, these always stop for the operator.

C1 denies them with a reason the model sees; C3 turns the same check into an approval card.
Deliberately conservative (a false stop costs one approval; a missed one can merge or rewrite
history): a command is split on shell separators and each piece is checked."""
from __future__ import annotations

import re
from pathlib import Path

HOME = Path.home()
CONDUCTOR_DIR = Path("/Users/Manoj/calwbot/logs/conductor")
GATED_DIRS = (HOME / ".claude", HOME / "Library" / "LaunchAgents", Path("/Users/Manoj/calwbot/trader"),
              CONDUCTOR_DIR)
READ_TOOLS = {"Read": "file_path", "Grep": "path", "Glob": "path", "NotebookRead": "notebook_path"}
MEMORY_DIR = re.compile(r"^" + re.escape(str(HOME / ".claude" / "projects")) + r"/[^/]+/memory/")
GATED_NAMES = {"settings.json", "settings.local.json", "CLAUDE.md", ".claude.json", ".mcp.json"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
GIT_GATED = {"merge", "push", "pull", "rebase", "cherry-pick", "revert", "am", "filter-branch",
             "filter-repo", "update-ref", "replace"}
LAUNCHCTL_GATED = {"load", "unload", "bootstrap", "bootout", "remove", "enable", "disable",
                   "kickstart", "kill", "submit"}
NAVIGATOR_GATED = {"merge", "reject", "audited", "approve", "decline", "abandon"}
# any of these in a Bash command means it may write a protected file (reads get stopped too:
# the Read tool is the way to read them)
_PROTECTED_TEXT = re.compile(
    r"CLAUDE\.md|settings(\.local)?\.json|\.claude\.json|\.mcp\.json|\.git/hooks|Library/LaunchAgents"
    r"|\.claude/(settings|hooks|agents|commands|skills|output-styles)")
# ~/.claude/plugins holds skill helper files the conversation reads a lot: a command that only
# READS there passes (2026-11-02); anything that could write, run code or hide a command stays gated
_PLUGINS_TEXT = re.compile(r"\.claude/plugins")
# Allowlist, not blocklist (cold audit 2026-11-02 found 12 ways past a blocklist): every word must
# be a plain literal or single-quoted (bash never expands either), every program a bare name from
# this list, and every option one we know only reads.
_READ_PROGRAMS = {"ls", "cat", "head", "tail", "grep", "find", "wc", "stat", "sed", "cut", "echo",
                  "test", "cd", "pwd", "true", "git"}
_SEPARATORS = {";", "&&", "||", "|"}
_PLAIN = re.compile(r"^[A-Za-z0-9_./~:=,+%@-]+$")  # no $ { } * ? [ ] \ " ! ` ( ) < > & | ;
_STDERR_ONLY = re.compile(r"(?<=\s)2>(/dev/null|&1)(?=\s|$|;|\|)")
_VAR_USE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)(/[A-Za-z0-9_./~:=,+%@-]*)?$")
_ASSIGN_LITERAL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./~:,+%@][A-Za-z0-9_./~:=,+%@-]*)$")
_FIND_OK = {"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth", "-size", "-mtime",
            "-mmin", "-newer", "-print", "-print0", "-o", "-a", "-not", "-and", "-or", "-L", "-H"}
_SED_SCRIPT = re.compile(r"^(\d+(,\d+)?|\$|\d+,\$)?p$")  # print lines: 1,40p  5p  $p  10,$p
_GIT_READ = {"status", "log", "show", "diff", "rev-parse"}
_GIT_ARG_BAD = re.compile(r"^--(output|ext-diff|textconv|git-dir|work-tree|exec|upload-pack|config)")


def _word(tok: str) -> str | None:
    """The literal value of a lexed word, or None when bash could expand or reinterpret it."""
    if len(tok) >= 2 and tok[0] == tok[-1] == "'":
        inner = tok[1:-1]
        return inner if "'" not in inner else None
    return tok if _PLAIN.match(tok) else None


def _args_read_only(prog: str, args: list[str]) -> bool:
    if prog == "git":
        if not args or args[0].startswith("-"):
            return False  # no -C / -c / --git-dir: only the workspace's own repo, its own config
        sub, rest = args[0], args[1:]
        if any(_GIT_ARG_BAD.match(a) for a in rest):
            return False
        if sub in _GIT_READ:
            return True
        if sub == "branch":
            return all(a in ("--show-current", "--list", "-a", "-r", "-v", "-vv") for a in rest)
        return sub == "worktree" and rest == ["list"]
    if prog == "find":
        return all(not a.startswith("-") or a in _FIND_OK for a in args)
    if prog == "sed":
        flags = [a for a in args if a.startswith("-")]
        plain = [a for a in args if not a.startswith("-")]
        return set(flags) <= {"-n", "-E"} and bool(plain) and bool(_SED_SCRIPT.match(plain[0]))
    return True  # ls cat head tail grep wc stat cut echo test cd pwd true: no write/exec options


def _plugins_read_only(command: str) -> bool:
    """True only if every piece of the command is a known read-only program with only literal
    words, no output redirection, substitution, subshell or path escape. Unsure = False."""
    import shlex
    if "\n" in command or "`" in command or "\\" in command or ".." in command:
        return False
    # stderr-only redirects, written exactly as ' 2>/dev/null' or ' 2>&1', are the only ones allowed
    command = _STDERR_ONLY.sub(" ", command)
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)
    lex.commenters = ""  # '#' is literal: a comment can't hide the rest of the command from us
    lex.wordchars += "~-./=,+%@:$*?[]{}!"  # keep these inside one word, then judge the whole word
    try:
        tokens = list(lex)
    except ValueError:
        return False
    pieces: list[list[str]] = [[]]
    known: dict[str, str] = {}  # NAME=literal set earlier in this command (S=/path; ls $S/x)
    for tok in tokens:
        if tok in _SEPARATORS:
            if len(pieces[-1]) == 1 and (a := _ASSIGN_LITERAL.match(pieces[-1][0])):
                known[a.group(1)] = a.group(2)  # usable by the pieces after this one
            pieces.append([])
            continue
        if m := _VAR_USE.match(tok):
            if m.group(1) not in known:
                return False  # $X we didn't see set to a plain literal: could be anything
            tok = known[m.group(1)] + (m.group(2) or "")
        word = _word(tok)
        if word is None:
            return False  # redirection, operator, expansion, double quotes, glob…
        pieces[-1].append(word)
    progs = {w[0] for w in pieces if w}
    if "cd" in progs and "git" in progs:
        return False  # git in a repo we cd'd into runs that repo's own config (diff drivers, pager…)
    for words in pieces:
        if not words:
            continue
        if len(words) == 1 and (a := _ASSIGN_LITERAL.match(words[0])):
            known[a.group(1)] = a.group(2)  # a plain literal not starting with '-': expands to itself
            continue
        prog = words[0]
        if "/" in prog or "=" in prog or prog not in _READ_PROGRAMS:
            return False  # a path (./ls, /tmp/x/cat), VAR=x, or a program we don't know
        if not _args_read_only(prog, words[1:]):
            return False
    return True


# the conversation must never reach its own approval channels (approve its own card, read or
# forge cards.json, restart the conductor to reload a forged file)
_CONDUCTOR_TEXT = re.compile(r"navigator\.conductor|conductor\.sock|logs/conductor|com\.calwbot\.conductor"
                             r"|api/v1/chat|cards\.json|tools/chat\.sh")
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
    if m := _CONDUCTOR_TEXT.search(command):
        return f"touches the chat service's own approvals ({m.group(0)})"
    if m := _PROTECTED_TEXT.search(command):
        return f"touches {m.group(0)} (permissions, hooks or services)"
    if (m := _PLUGINS_TEXT.search(command)) and not _plugins_read_only(command):
        return f"touches {m.group(0)} (permissions, hooks or services)"
    tokens =[t for t in _SPLIT.split(command.replace('"', " ").replace("'", " ")) if t]
    for i, tok in enumerate(tokens):
        prog = tok.rsplit("/", 1)[-1]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if prog == "git" and (r := _git_reason(tokens, i)):
            return r
        if prog == "launchctl" and nxt in LAUNCHCTL_GATED:
            return f"launchctl {nxt} installs or removes a service"
        if prog == "claude":
            return "starts another Claude, which runs outside this conversation's approval gate"
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
    key = READ_TOOLS.get(tool_name)
    if key and tool_input.get(key):
        p = Path(str(tool_input[key])).expanduser()
        p = (p if p.is_absolute() else Path(cwd) / p).resolve()
        if p == CONDUCTOR_DIR or CONDUCTOR_DIR in p.parents:
            return "reads the chat service's approval state"
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


# no code or id here: the model must never hold what approves its own card
CARD_HINT = ("an approval card was sent to the operator (website, Telegram, terminal). Stop and wait; "
             "if it is approved you will be told to run exactly the same command again, in the same folder.")
TOO_LONG_HINT = ("too long to show the operator on an approval card; split it into shorter steps so the "
                 "gated part can be shown in full")


def make_hook(cards, on_card):
    """C3: a gated call raises an approval card instead of a plain deny; the one identical call
    after the operator approves it is allowed (explicitly, so no permission prompt can stop it)."""
    async def hook(input_data: dict, tool_use_id, context) -> dict:
        from navigator.conductor.cards import CardError
        name, tool_input = input_data.get("tool_name", ""), input_data.get("tool_input") or {}
        cwd = str(input_data.get("cwd") or "/Users/Manoj/calwbot")
        reason = gated_reason(name, tool_input, Path(cwd))
        if reason is None:
            return {}
        used = cards.consume(name, tool_input, cwd=cwd)
        if used is not None:
            on_card(used)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "permissionDecisionReason": f"approval card {used['code']} approved"}}
        try:
            card, new = cards.raise_card(name, tool_input, reason, cwd=cwd)
        except CardError:
            hint = TOO_LONG_HINT
        else:
            hint = CARD_HINT
            if new:
                on_card(card)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"{reason} — {hint}"}}
    return hook
