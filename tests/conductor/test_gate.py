import asyncio
from pathlib import Path

import pytest

from navigator.conductor.gate import can_use_tool, gated_reason, pre_tool_use_hook

CWD = Path("/Users/Manoj/calwbot")
HOME = Path.home()


@pytest.mark.parametrize("cmd", [
    "git merge x", "git -C /r merge x", "git -c a=b push", "cd x && git rebase main",
    'bash -c "git reset --hard"', "git reset --hard HEAD~1", "git commit --amend -m x",
    "git filter-branch --all", "launchctl bootout gui/504/x", "launchctl load ~/Library/LaunchAgents/a.plist",
    "cat trader/state.json > /dev/null", "ls; git push origin main", "echo hi | git merge y",
    "cp a.plist ~/Library/LaunchAgents/", "git update-ref refs/heads/main abc",
])
def test_bash_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD)


@pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "ls", "grep merge README.md",
                                 "git diff main..x", "launchctl list", "git reset HEAD file.py"])
def test_bash_not_gated(cmd):
    assert gated_reason("Bash", {"command": cmd}, CWD) is None


@pytest.mark.parametrize("path", [str(HOME / ".claude/settings.json"), "CLAUDE.md", "navigator/../trader/x.py",
                                  str(HOME / "Library/LaunchAgents/a.plist"), "x/settings.local.json",
                                  str(HOME / ".claude/hooks/x.sh")])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_write_gated(tool, path):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert gated_reason(tool, {key: path}, CWD)


def test_write_not_gated():
    assert gated_reason("Write", {"file_path": "docs/x.md"}, CWD) is None
    assert gated_reason("Read", {"file_path": "trader/x.py"}, CWD) is None
    # Claude's own memory notes are normal work, not permissions
    assert gated_reason("Write", {"file_path": str(HOME / ".claude/projects/m/memory/x.md")}, CWD) is None
    assert gated_reason("Write", {"file_path": str(HOME / ".claude/projects/m/memory/settings.json")}, CWD)


def test_hook_shape_deny():
    out = asyncio.run(pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "git merge x"},
                                         "cwd": str(CWD)}, "t1", None))
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    assert "operator" in h["permissionDecisionReason"]
    assert asyncio.run(pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t2", None)) == {}


def test_can_use_tool_denies():
    r = asyncio.run(can_use_tool("Bash", {"command": "rm -rf x"}, None))
    assert r.behavior == "deny" and "operator" in r.message
