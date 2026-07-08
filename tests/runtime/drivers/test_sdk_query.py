import sys
import types
import pytest
from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.base import DriverRequest
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard

def _stub_sdk(monkeypatch, captured, results=('{"commands": [], "summary": "ok"}',)):
    mod = types.ModuleType("claude_agent_sdk")
    class ResultMessage:
        def __init__(self, result=None):
            self.result = result
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            captured["options"] = kw
    async def query(*, prompt, options):
        captured["prompt"] = prompt
        for r in results:
            yield ResultMessage(result=r)
    mod.query = query
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)

def test_query_fn_returns_result_text_with_locked_down_options(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn()
    assert fn("hello") == '{"commands": [], "summary": "ok"}'
    assert captured["prompt"] == "hello"
    # 2, not 1: the CLI occasionally burns a turn before the reply; observed live 2026-07-08
    assert captured["options"]["max_turns"] == 2
    assert captured["options"]["allowed_tools"] == []
    assert "JSON" in captured["options"]["system_prompt"]
    # no filesystem settings: without this the session inherits the operator's
    # hooks/plugins/skills (observed live 2026-07-08: claude-mem hooks fired,
    # skill reminders burned turns until max_turns errored)
    assert captured["options"]["setting_sources"] == []

def test_model_passed_through(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    make_sdk_query_fn(model="claude-haiku-4-5-20251001")("x")
    assert captured["options"]["model"] == "claude-haiku-4-5-20251001"

def test_missing_sdk_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(ValueError, match="claude-agent-sdk"):
        make_sdk_query_fn()

def test_no_result_message_raises(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured, results=())
    with pytest.raises(RuntimeError, match="no result"):
        make_sdk_query_fn()("x")

def test_markdown_fences_stripped_at_adapter_boundary(monkeypatch):
    # live models fence JSON despite instructions; the adapter must hand the
    # driver bare JSON so its strict parser stays strict
    captured = {}
    fenced = '```json\n{"commands": [], "summary": "ok"}\n```'
    _stub_sdk(monkeypatch, captured, results=(fenced,))
    assert make_sdk_query_fn()("x") == '{"commands": [], "summary": "ok"}'

def test_plain_fences_without_language_tag_stripped(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured, results=('```\n{"a": 1}\n```',))
    assert make_sdk_query_fn()("x") == '{"a": 1}'

def test_driver_end_to_end_with_stubbed_sdk(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured, results=('{"commands": [["uv","run","pytest","-q"]], "summary": "verify"}',))
    d = ClaudeSDKDriver(query_fn=make_sdk_query_fn(), budget_guard=BudgetGuard(2.0))
    result = d.run_role(DriverRequest(role=Role.verifier, context={"repo": "trader"}, worktree_path="/tmp/w"))
    assert result.commands == [["uv", "run", "pytest", "-q"]]
    assert d.budget_guard.spent_usd == 0.25
    assert "verifier" in captured["prompt"]
