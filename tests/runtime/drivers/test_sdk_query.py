import json
import re
import sys
import types
from datetime import datetime, timezone
import pytest
from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
from navigator.safety.errors import QuotaExhaustedError
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
    # headroom over 1: the CLI occasionally burns turns before the reply
    # (observed live 2026-07-08 at 1, 2026-07-18 at 2); tools are disabled so
    # unused turns cost nothing
    assert captured["options"]["max_turns"] == 4
    assert captured["options"]["allowed_tools"] == []
    # allowed_tools=[] only withholds auto-approval; the model still SEES the default
    # toolset, tries tools, and burns turns (live error_max_turns 2026-10-10).
    # tools=[] is what removes them (--tools "")
    assert captured["options"]["tools"] == []
    # claude.ai connectors (Docs incl. delete, Uber Eats, job boards) were still
    # offered to subagents; they are disabled outright
    assert json.loads(captured["options"]["settings"]) == {"disableClaudeAiConnectors": True}
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

def test_no_result_message_retried_once_then_raises(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured, results=())
    calls = _count_queries(monkeypatch)
    with pytest.raises(RuntimeError, match="no result"):
        make_sdk_query_fn()("x")
    assert calls["n"] == 2  # bounded: exactly one retry, then surface


def _stub_sdk_sequence(monkeypatch, captured, attempts):
    """Stub whose query behaves per-call: each entry of `attempts` is either an
    Exception to raise or a tuple of result strings to yield."""
    mod = types.ModuleType("claude_agent_sdk")
    class ResultMessage:
        def __init__(self, result=None):
            self.result = result
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            captured["options"] = kw
    calls = {"n": 0}
    async def query(*, prompt, options):
        step = attempts[min(calls["n"], len(attempts) - 1)]
        calls["n"] += 1
        if isinstance(step, Exception):
            raise step
        for r in step:
            yield ResultMessage(result=r)
    mod.query = query
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return calls

def _count_queries(monkeypatch):
    import claude_agent_sdk as mod
    calls = {"n": 0}
    real = mod.query
    def counting(*, prompt, options):
        calls["n"] += 1
        return real(prompt=prompt, options=options)
    monkeypatch.setattr(mod, "query", counting)
    return calls

def test_max_turns_error_result_retried_once(monkeypatch):
    # observed live 2026-07-18: fable-5 burned every turn without a result and
    # the SDK raised "Claude Code returned an error result: Reached maximum
    # number of turns (2)" — one bounded retry recovers the cycle
    captured = {}
    good = '{"commands": [], "summary": "ok"}'
    calls = _stub_sdk_sequence(monkeypatch, captured, [
        Exception("Claude Code returned an error result: Reached maximum number of turns (4)"),
        (good,)])
    assert make_sdk_query_fn()("x") == good
    assert calls["n"] == 2

def test_non_retryable_exception_propagates_without_retry(monkeypatch):
    captured = {}
    calls = _stub_sdk_sequence(monkeypatch, captured, [ValueError("bad options")])
    with pytest.raises(ValueError, match="bad options"):
        make_sdk_query_fn()("x")
    assert calls["n"] == 1

def test_truncated_json_retried_once_then_recovers(monkeypatch):
    # observed live 2026-07-18: fable-5 reply cut mid-generation (no closing
    # brace) → DriverParseError; a fresh query usually completes
    captured = {}
    good = '{"commands": [], "summary": "ok"}'
    calls = _stub_sdk_sequence(monkeypatch, captured, [
        ('{"proposals": [{"title": "truncat',), (good,)])
    assert make_sdk_query_fn()("x") == good
    assert calls["n"] == 2

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

def test_fenced_json_embedded_in_prose_extracted(monkeypatch):
    # observed live 2026-07-15: fable-5 planner cycle failed DriverParseError;
    # models sometimes wrap the fenced block in prose despite the system prompt
    captured = {}
    wrapped = 'Here is the plan:\n```json\n{"commands": [], "summary": "ok"}\n```\nLet me know.'
    _stub_sdk(monkeypatch, captured, results=(wrapped,))
    assert make_sdk_query_fn()("x") == '{"commands": [], "summary": "ok"}'

def test_bare_json_embedded_in_prose_extracted(monkeypatch):
    captured = {}
    wrapped = 'Sure!\n{"commands": [], "summary": "ok"}\nDone.'
    _stub_sdk(monkeypatch, captured, results=(wrapped,))
    assert make_sdk_query_fn()("x") == '{"commands": [], "summary": "ok"}'

def test_truncated_json_passes_through_for_driver_error(monkeypatch):
    # no closing brace on BOTH attempts: adapter must not invent structure;
    # the driver's strict parser reports it with head/tail detail
    captured = {}
    _stub_sdk(monkeypatch, captured, results=('{"commands": [',))
    calls = _count_queries(monkeypatch)
    assert make_sdk_query_fn()("x") == '{"commands": ['
    assert calls["n"] == 2  # retried once, still truncated, surfaced as-is

def test_hung_sdk_query_raises_instead_of_blocking_forever(monkeypatch):
    # observed live 2026-07-17: the CLI subprocess stalled during init under
    # launchd and asyncio.run blocked the daemon on cycle 1 for 19 hours; a
    # stall must surface as an exception so the loop retries next tick
    captured = {}
    mod = types.ModuleType("claude_agent_sdk")
    class ResultMessage:
        def __init__(self, result=None):
            self.result = result
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            captured["options"] = kw
    async def query(*, prompt, options):
        import asyncio
        await asyncio.sleep(3600)
        yield ResultMessage(result="never")
    mod.query = query
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    fn = make_sdk_query_fn(timeout_s=0.1)
    with pytest.raises(TimeoutError, match="timed out"):
        fn("x")

def test_driver_end_to_end_with_stubbed_sdk(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured, results=('{"commands": [["uv","run","pytest","-q"]], "summary": "verify"}',))
    d = ClaudeSDKDriver(query_fn=make_sdk_query_fn(), budget_guard=BudgetGuard(2.0))
    result = d.run_role(DriverRequest(role=Role.verifier, context={"repo": "trader"}, worktree_path="/tmp/w"))
    assert result.commands == [["uv", "run", "pytest", "-q"]]
    assert d.budget_guard.spent_usd == 0.25
    assert "verifier" in captured["prompt"]


def _stub_sdk_error_result(monkeypatch, text):
    """Mimics the real CLI: an is_error ResultMessage carrying the actual
    reason, then the SDK's generic 'error result: success' exception."""
    mod = types.ModuleType("claude_agent_sdk")
    class ResultMessage:
        def __init__(self, result=None, is_error=False):
            self.result = result
            self.is_error = is_error
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            pass
    calls = {"n": 0}
    async def query(*, prompt, options):
        calls["n"] += 1
        yield ResultMessage(result=text, is_error=True)
        raise Exception("Claude Code returned an error result: success")
    mod.query = query
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return calls

def test_error_result_surfaces_real_reason_not_generic_success(monkeypatch):
    # observed live 2026-08-25 → 2026-10-09: 216 cycles logged only
    # "error result: success"; the real reason was in ResultMessage.result
    _stub_sdk_error_result(monkeypatch, "Usage credits are required for this model.")
    with pytest.raises(RuntimeError, match="Usage credits are required"):
        make_sdk_query_fn()("x")

def test_billing_and_model_support_errors_not_retried(monkeypatch):
    for text in ("Usage credits are required for this model.",
                 "API Error: 400 Claude Code 2.1.165 does not support this model; "
                 "version 2.1.280 or newer is required."):
        calls = _stub_sdk_error_result(monkeypatch, text)
        with pytest.raises(RuntimeError):
            make_sdk_query_fn()("x")
        assert calls["n"] == 1

def test_transient_error_result_still_retried_once(monkeypatch):
    calls = _stub_sdk_error_result(monkeypatch, "Reached maximum number of turns (4)")
    with pytest.raises(RuntimeError, match="maximum number of turns"):
        make_sdk_query_fn()("x")
    assert calls["n"] == 2


def test_error_result_without_text_reports_subtype_and_errors(monkeypatch):
    # observed live 2026-10-09 under launchd: is_error with result=None logged as
    # "error result: None" — the reason lives in subtype/errors
    mod = types.ModuleType("claude_agent_sdk")
    class ResultMessage:
        def __init__(self):
            self.result = None; self.is_error = True
            self.subtype = "error_during_execution"; self.errors = ["API Error: 529 overloaded"]
    class ClaudeAgentOptions:
        def __init__(self, **kw): pass
    async def query(*, prompt, options):
        yield ResultMessage()
        raise Exception("Claude Code returned an error result: success")
    mod.query, mod.ClaudeAgentOptions, mod.ResultMessage = query, ClaudeAgentOptions, ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    with pytest.raises(RuntimeError) as exc:
        make_sdk_query_fn()("x")
    assert "error_during_execution" in str(exc.value) and "529 overloaded" in str(exc.value)

def test_usage_limit_errors_are_not_retried(monkeypatch):
    calls = _stub_sdk_error_result(monkeypatch, "You've hit your session limit · resets 10:30pm")
    with pytest.raises(RuntimeError, match="session limit"):
        make_sdk_query_fn()("x")
    assert calls["n"] == 1

def test_trailing_prose_after_json_is_dropped(monkeypatch):
    # live 2026-10-11: Sonnet verifiers replied '{"commands": ...} <explanation>' and
    # 3 waker runs died on "response is not valid JSON"
    captured = {}
    _stub_sdk(monkeypatch, captured, results=(
        '{"commands": [["git", "status"]], "summary": "s"}\nNote: "uv run --extra dev pytest" matches.',))
    assert make_sdk_query_fn()("x") == '{"commands": [["git", "status"]], "summary": "s"}'


def test_session_limit_without_fallback_is_quota_exhausted(monkeypatch):
    # N16: no fallback chain, a reset time in the text: one call, QuotaExhaustedError with
    # a resets_at clamped like the cooldown ([60s, 6h] from now)
    calls = _stub_sdk_error_result(monkeypatch, "You've hit your session limit \u00b7 resets 10:30pm")
    with pytest.raises(QuotaExhaustedError) as exc:
        make_sdk_query_fn()("x")
    assert calls["n"] == 1
    assert exc.value.marker == "session limit"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", exc.value.resets_at)
    remaining = (datetime.fromisoformat(exc.value.resets_at)
                 - datetime.now(timezone.utc)).total_seconds()
    assert 60 <= remaining <= 6 * 3600 + 5
    # no reset time and no fallback chain: the reset is unknown
    _stub_sdk_error_result(monkeypatch, "You've hit your session limit")
    with pytest.raises(QuotaExhaustedError) as exc:
        make_sdk_query_fn()("x")
    assert exc.value.resets_at is None
    # per-model credits are never an account-wide quota
    _stub_sdk_error_result(monkeypatch, "Usage credits are required for this model.")
    with pytest.raises(RuntimeError) as exc:
        make_sdk_query_fn()("x")
    assert not isinstance(exc.value, QuotaExhaustedError)


# --- builder tools (operator decision 2026-10-28): read-only tools inside the worktree -----

# EXACTLY the deny list of sdk_query._TOOLED_SETTINGS, in order: an added, dropped or
# reordered entry fails test_tooled_options_set_cwd_tools_turns_and_deny_list
DENY = [
    "Read(//Users/**)", "Read(//System/Volumes/Data/**)", "Read(//private/var/folders/**)",
    "Read(//private/tmp/claude-*/**)", "Read(//tmp/claude-*/**)",
    "Read(**/.env*)", "Read(**/*.pem)", "Read(**/*.key)",
    "Grep(//Users/**)", "Grep(//System/Volumes/Data/**)", "Grep(//private/var/folders/**)",
    "Grep(//private/tmp/claude-*/**)", "Grep(//tmp/claude-*/**)",
    "Glob(//Users/**)", "Glob(//System/Volumes/Data/**)", "Glob(//private/var/folders/**)",
    "Glob(//private/tmp/claude-*/**)", "Glob(//tmp/claude-*/**)",
    "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
]


def test_tool_less_options_are_unchanged_even_when_cwd_is_passed(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn()
    assert fn("hello", cwd="/tmp/wt") == '{"commands": [], "summary": "ok"}'
    opts = captured["options"]
    assert "cwd" not in opts
    assert opts["tools"] == [] and opts["allowed_tools"] == [] and opts["max_turns"] == 4
    assert json.loads(opts["settings"]) == {"disableClaudeAiConnectors": True}
    assert "Read" not in opts["system_prompt"]


def test_tooled_options_set_cwd_tools_turns_and_deny_list(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn(tools=("Read", "Grep", "Glob"), max_turns=30)
    fn("hello", cwd="/private/tmp/navigator-worktrees/run-x")
    opts = captured["options"]
    assert opts["cwd"] == "/private/tmp/navigator-worktrees/run-x"
    assert opts["tools"] == ["Read", "Grep", "Glob"]
    assert opts["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert opts["max_turns"] == 30
    assert opts["setting_sources"] == []
    settings = json.loads(opts["settings"])
    assert len(DENY) == 24
    assert settings == {"disableClaudeAiConnectors": True, "permissions": {"deny": DENY}}
    assert "JSON" in opts["system_prompt"] and "Read" in opts["system_prompt"]
    assert opts["system_prompt"].startswith("You are a Navigator subagent")


def test_tools_without_cwd_fall_back_to_tool_less_options(monkeypatch):
    # NEGATIVE: tools alone never change the session; only a call that passes cwd does
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn(tools=("Read",), max_turns=30)
    fn("hello")
    opts = captured["options"]
    assert "cwd" not in opts and opts["tools"] == [] and opts["max_turns"] == 4
    assert json.loads(opts["settings"]) == {"disableClaudeAiConnectors": True}


def test_max_turns_never_changes_the_tool_less_options(monkeypatch):
    # NEGATIVE: the tool-less options keep the literal 4 whatever max_turns was passed
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn(max_turns=99)
    fn("hello")
    assert captured["options"]["max_turns"] == 4
    assert captured["options"]["tools"] == []


def test_cwd_is_keyword_only(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    with pytest.raises(TypeError):
        make_sdk_query_fn()("hello", "/tmp/wt")


# --- builder tests (build 2): in-process MCP servers join the tooled builder session only --

def test_tooled_options_pass_mcp_servers_and_extend_allowed_tools(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn(tools=("Read", "Grep", "Glob"), max_turns=30)
    fn("hello", cwd="/private/tmp/navigator-worktrees/run-x", mcp_servers={"navigator": "SRV"},
       mcp_tools=("mcp__navigator__run_tests",))
    opts = captured["options"]
    assert opts["mcp_servers"] == {"navigator": "SRV"}
    assert opts["tools"] == ["Read", "Grep", "Glob"]
    assert opts["allowed_tools"] == ["Read", "Grep", "Glob", "mcp__navigator__run_tests"]
    assert opts["cwd"] == "/private/tmp/navigator-worktrees/run-x"
    assert opts["max_turns"] == 30
    assert opts["setting_sources"] == []
    assert json.loads(opts["settings"]) == {"disableClaudeAiConnectors": True,
                                            "permissions": {"deny": DENY}}
    assert opts["system_prompt"].startswith("You are a Navigator subagent")


def test_tooled_options_without_mcp_kwargs_are_build_1_options(monkeypatch):
    # NEGATIVE: cwd alone never adds an MCP server or an extra allowed tool
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn(tools=("Read", "Grep", "Glob"), max_turns=30)
    fn("hello", cwd="/private/tmp/navigator-worktrees/run-x")
    opts = captured["options"]
    assert "mcp_servers" not in opts
    assert opts["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert opts["tools"] == ["Read", "Grep", "Glob"] and opts["max_turns"] == 30
    assert opts["cwd"] == "/private/tmp/navigator-worktrees/run-x"
    assert json.loads(opts["settings"]) == {"disableClaudeAiConnectors": True,
                                            "permissions": {"deny": DENY}}


def test_tool_less_options_ignore_mcp_kwargs(monkeypatch):
    # NEGATIVE: no tools means no session tools at all, whatever the call passes
    captured = {}
    _stub_sdk(monkeypatch, captured)
    fn = make_sdk_query_fn()
    fn("hello", cwd="/tmp/wt", mcp_servers={"navigator": "SRV"},
       mcp_tools=("mcp__navigator__run_tests",))
    opts = captured["options"]
    assert "cwd" not in opts and "mcp_servers" not in opts
    assert opts["tools"] == [] and opts["allowed_tools"] == [] and opts["max_turns"] == 4
    assert json.loads(opts["settings"]) == {"disableClaudeAiConnectors": True}


def test_mcp_kwargs_are_keyword_only(monkeypatch):
    captured = {}
    _stub_sdk(monkeypatch, captured)
    with pytest.raises(TypeError):
        make_sdk_query_fn()("hello", "/tmp/wt", {})
