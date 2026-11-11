"""Pins the fact that a helper cannot start other helpers, subagents or builds (2026-11-09).

Configuration only: it inspects the options object, never starts the CLI, the SDK or the network.
If you are adding helper fan-out, set max 3 subagents per helper and depth 1 first. Deferred cap (not
coded): if `Agent` is ever added, `agents` is capped at 3 per helper, depth 1, via a PreToolUse count
in helpers.py."""
import pytest

from navigator.conductor import helpers as H

WHY = "if you are adding helper fan-out, set max 3 subagents per helper and depth 1 first"
BANNED = ("Agent", "Task")


async def _hook(input_data, tool_use_id, context):
    return {}


def _bad(name: str) -> bool:
    return name in BANNED or name.startswith("mcp__conductor__")


def test_helper_tools_have_no_fanout():
    assert not [t for t in H.HELPER_TOOLS if _bad(t)], WHY


def _variants(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    yield "plain", H.helper_options(_hook)
    yield "exact", H.helper_options(_hook, exact={"tool": "Bash", "input": {"command": "ls"}, "cwd": "/tmp"})
    monkeypatch.setenv("CONDUCTOR_HELPER_ORC", "0")
    yield "orc-off", H.helper_options(_hook)


def test_options_have_no_fanout(monkeypatch):
    seen = 0
    for name, o in _variants(monkeypatch):
        seen += 1
        assert not [t for t in (o.tools or []) if _bad(t)], f"{name}: {WHY}"
        assert "conductor" not in (o.mcp_servers or {}), f"{name}: {WHY}"
        assert not o.agents, f"{name}: {WHY}"
        assert o.max_turns == H.HELPER_MAX_TURNS == 40, f"{name}: {WHY}"
    assert seen == 3


@pytest.mark.parametrize("name,limit", [("HARD_MAX_RUNNING", 8), ("HELPER_MAX_TIMEOUT_S", 1800)])
def test_caps_not_raised(name, limit):
    assert getattr(H, name) <= limit, WHY
