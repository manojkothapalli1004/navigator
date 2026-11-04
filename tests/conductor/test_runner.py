import asyncio

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from navigator.conductor import runner
from navigator.conductor.engine import ResumeFailed


def _result(sid="s-new", is_error=False, result="ok"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id=sid, result=result)


def _delta(text):
    return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "index": 0,
                                                         "delta": {"type": "text_delta", "text": text}})


class FakeClient:
    messages: list = []
    raise_on_query: Exception | None = None
    seen_options = None
    def __init__(self, options):
        FakeClient.seen_options = options
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def query(self, prompt):
        if FakeClient.raise_on_query:
            raise FakeClient.raise_on_query
    async def receive_response(self):
        for m in FakeClient.messages:
            yield m


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(runner, "ClaudeSDKClient", FakeClient)
    FakeClient.messages, FakeClient.raise_on_query = [], None


def test_streams_deltas_and_returns_session_id():
    FakeClient.messages = [_delta("he"), _delta("llo"), _result("s-9")]
    out = []
    sid = asyncio.run(runner.sdk_turn("hi", "s-1", out.append))
    assert sid == "s-9" and [e["text"] for e in out] == ["he", "llo"]
    o = FakeClient.seen_options
    assert o.resume == "s-1" and o.include_partial_messages and o.permission_mode == "auto"
    assert o.model == "claude-opus-5-5" and o.setting_sources == ["user", "project", "local"]
    assert str(o.cwd) == "/Users/Manoj/calwbot" and o.can_use_tool is not None and o.hooks["PreToolUse"]


def test_runner_maps_session_not_found_to_resume_failed():
    FakeClient.raise_on_query = RuntimeError("No conversation found with session ID: s-1")
    with pytest.raises(ResumeFailed):
        asyncio.run(runner.sdk_turn("hi", "s-1", lambda e: None))


def test_session_not_found_without_resume_is_plain_error():
    FakeClient.raise_on_query = RuntimeError("No conversation found with session ID: x")
    with pytest.raises(RuntimeError):
        asyncio.run(runner.sdk_turn("hi", None, lambda e: None))


def test_error_result_raises():
    FakeClient.messages = [_result(is_error=True, result="rate limited")]
    with pytest.raises(RuntimeError, match="rate limited"):
        asyncio.run(runner.sdk_turn("hi", None, lambda e: None))


def test_text_blocks_separated():
    start = StreamEvent(uuid="u", session_id="s", event={"type": "content_block_start", "index": 1,
                                                          "content_block": {"type": "text", "text": ""}})
    FakeClient.messages = [_delta("a"), start, _delta("b"), _result()]
    out = []
    asyncio.run(runner.sdk_turn("hi", None, out.append))
    assert "".join(e["text"] for e in out) == "a\n\nb"


def test_options_append_windows_prompt():
    o = runner.options(None)
    assert o.system_prompt == {"type": "preset", "preset": "claude_code", "append": runner.WINDOWS_PROMPT}
    for word in ("[web]", "[telegram]", "[terminal]", "[open:/navigator/decisions]"):
        assert word in runner.WINDOWS_PROMPT


def test_permission_mode_matches_terminal_auto():
    # the operator's terminal runs defaultMode "auto" (~/.claude/settings.json); "default" made
    # every prompt-worthy action (ORC read, ACT read, file write) a denial in a window with no human
    assert runner.options(None).permission_mode == "auto"


# ---- helpers stage 1 (2026-11-04): the main turn only talks and delegates ----

def test_main_turn_tools_are_restricted():
    o = runner.options(None)
    assert o.tools == runner.MAIN_TOOLS and o.allowed_tools == []
    for t in ("Read", "Bash", "Agent", "Task", "Edit", "Write", "WebFetch", "Skill"):
        assert t not in o.tools
    assert {"mcp__conductor__delegate", "mcp__conductor__helpers", "mcp__conductor__cancel_helper",
            "mcp__orchestrator__get_canonical_memory"} <= set(o.tools)
    assert not any(t.startswith("mcp__alpaca") for t in o.tools)


def test_main_turn_loads_only_named_mcp():
    o = runner.options(None)
    assert o.strict_mcp_config is True and set(o.mcp_servers) == {"orchestrator"}
    assert o.mcp_servers["orchestrator"] == runner.ORC_MCP and runner.ORC_MCP["type"] == "stdio"
    assert o.disallowed_tools == runner.ORC_WRITE_TOOLS
    for name in ("apply_memory_update", "create_idea", "add_idea_note", "convert_idea", "finalize_idea",
                 "save_prompt", "update_prompt_status"):
        assert f"mcp__orchestrator__{name}" in runner.ORC_WRITE_TOOLS
        assert f"mcp__orchestrator__{name}" not in runner.MAIN_TOOLS
    fake = {"type": "sdk", "name": "conductor", "instance": object()}
    with_server = runner.options("s-1", conductor_server=fake)
    assert set(with_server.mcp_servers) == {"orchestrator", "conductor"} and with_server.mcp_servers["conductor"] is fake
    assert with_server.resume == "s-1"


def test_sdk_turn_passes_conductor_server_through():
    FakeClient.messages = [_result("s-2")]
    fake = {"type": "sdk", "name": "conductor", "instance": object()}
    asyncio.run(runner.sdk_turn("hi", None, lambda e: None, conductor_server=fake))
    assert FakeClient.seen_options.mcp_servers["conductor"] is fake


def test_windows_prompt_tells_the_model_to_delegate():
    for phrase in ("call delegate", "Check helpers first", "[helper", "Answer 'how is it going' from helpers"):
        assert phrase in runner.WINDOWS_PROMPT
