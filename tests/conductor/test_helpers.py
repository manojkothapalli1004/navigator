"""Helpers (2026-11-04): the work a turn used to do itself runs beside the conversation."""
import asyncio
import functools
import json
import time

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, ToolUseBlock

from navigator.conductor import helpers as H
from navigator.conductor import tools
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store


def aio(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


class Recorder:
    """A runner that waits on an Event (or returns at once) and remembers how it was called."""

    def __init__(self, result="done", gate=None, raise_exc=None):
        self.result, self.gate, self.raise_exc, self.calls = result, gate, raise_exc, []

    async def __call__(self, task, helper_id, emit, exact=None):
        self.calls.append((task, helper_id, exact))
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_exc:
            raise self.raise_exc
        return self.result


def _helpers(tmp_path, runner, finished=None, events=None, **kw):
    finished = finished if finished is not None else []
    events = events if events is not None else []
    return H.Helpers(tmp_path / "helpers.json", runner,
                     on_event=lambda hid, ev: events.append((hid, ev)),
                     on_finish=finished.append, **kw)


def _file(tmp_path):
    return json.loads((tmp_path / "helpers.json").read_text())["helpers"]


async def _settle(n=100):
    for _ in range(n):
        await asyncio.sleep(0.005)


TASK = "Read navigator/conductor/engine.py and report what the worker loop does, in three lines."


@aio
async def test_delegate_starts_helper_and_turn_ends(tmp_path):
    gate = asyncio.Event()
    hs = _helpers(tmp_path, Recorder(gate=gate))
    reply = await tools.delegate_text(hs, {"title": "read engine", "task": TASK})
    assert reply.startswith("started helper-") and "(running)" in reply and reply.endswith(": read engine")
    recs = _file(tmp_path)
    assert len(recs) == 1 and recs[0]["state"] == "running" and recs[0]["task"] == TASK
    gate.set(); await _settle(10)
    assert _file(tmp_path)[0]["state"] == "done"


@aio
async def test_delegate_refuses_short_task(tmp_path):
    hs = _helpers(tmp_path, Recorder())
    assert (await tools.delegate_text(hs, {"title": "fix", "task": "fix it"})).startswith("refused")
    assert (await tools.delegate_text(hs, {"title": "", "task": TASK})).startswith("refused")
    assert not (tmp_path / "helpers.json").exists() and hs.snapshot() == []


@aio
async def test_finish_submits_helper_message(tmp_path):
    for via, expect in (("telegram", "telegram"), (None, None)):
        calls = []
        eng = Engine(Store(tmp_path / f"c{via}"), None, lock_path=tmp_path / "turn.lock")
        hs = _helpers(tmp_path / f"h{via}", Recorder(result="found 3 files"))
        eng.helpers, hs.on_finish = hs, eng.on_helper_finish
        eng.submit = lambda *a, **k: calls.append((a, k))
        hs.current_via = via
        rec = hs.start("count files", TASK)
        await _settle(10)
        assert len(calls) == 1
        (text, source), kw = calls[0]
        assert source == "helper" and kw == {"internal": True, "reply_via": expect}
        assert text.startswith(f"[helper {rec['id']} done: count files]\n") and "found 3 files" in text
        saved = _file(tmp_path / f"h{via}")[0]
        assert saved["state"] == "done" and saved["ended"] and saved["result"] == "found 3 files"


@aio
async def test_result_is_scrubbed_and_capped(tmp_path):
    assert H.RESULT_CHARS == 12000
    words = " ".join(["word"] * 4000)
    hs = _helpers(tmp_path, Recorder(result="token sk-abcdefghijkl here " + words))
    hs.start("leaky", TASK); await _settle(10)
    saved = _file(tmp_path)[0]["result"]
    assert "sk-abcdefghijkl" not in saved and saved.startswith("token … here")
    assert saved.endswith("characters]") and "[… report cut at" in saved
    assert saved.startswith(("token … here " + words)[:H.RESULT_CHARS])
    whole = ("w" * 7 + " ") * 1499 + "abcdefg"  # 11_999 chars
    assert len(whole) == 11_999
    hs2 = _helpers(tmp_path / "b", Recorder(result=whole))
    hs2.start("whole", TASK); await _settle(10)
    assert _file(tmp_path / "b")[0]["result"] == whole
    late = ("w" * 7 + " ") * 1498 + "x sk-abcdefghijkl yyyy" + " pad" * 2000
    assert late.index("sk-abc") < 12000 < late.index("sk-abc") + 40 or True
    hs3 = _helpers(tmp_path / "c", Recorder(result=late))
    hs3.start("late", TASK); await _settle(10)
    assert "sk-abcdefghijkl" not in _file(tmp_path / "c")[0]["result"]


@aio
async def test_failure_and_timeout_are_reported(tmp_path, monkeypatch):
    finished = []
    hs = _helpers(tmp_path, Recorder(raise_exc=RuntimeError("boom")), finished=finished)
    hs.start("explode", TASK); await _settle(10)
    assert finished[0]["state"] == "failed" and finished[0]["error"] == "RuntimeError: boom"
    monkeypatch.setattr(H, "HELPER_TIMEOUT_S", 0.05)
    slow = _helpers(tmp_path / "slow", Recorder(gate=asyncio.Event()), finished=finished)
    slow.start("hang", TASK); await _settle(40)
    assert finished[1]["state"] == "failed" and "timed out" in finished[1]["error"]
    assert [r["state"] for r in _file(tmp_path / "slow")] == ["failed"]


@aio
async def test_concurrency_cap_queues_fourth(tmp_path):
    gates = [asyncio.Event() for _ in range(4)]
    runner = Recorder()
    calls = []

    async def run(task, helper_id, emit, exact=None):
        calls.append(helper_id)
        await gates[int(task[-1])].wait()
        return "ok"
    hs = _helpers(tmp_path, run, max_running=3)
    recs = [hs.start(f"job {i}", TASK + f" #{i}") for i in range(4)]
    assert [r["state"] for r in recs] == ["running", "running", "running", "queued"]
    await _settle(2)
    assert calls == [r["id"] for r in recs[:3]]
    gates[1].set(); await _settle(5)
    assert calls == [r["id"] for r in recs[:3]] + [recs[3]["id"]]
    assert [s["state"] for s in hs.snapshot()] == ["running", "running", "running", "done"]
    for g in gates:
        g.set()
    await _settle(5)
    assert all(r["state"] == "done" for r in _file(tmp_path))


@aio
async def test_cancel_running_helper(tmp_path):
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=finished)
    rec = hs.start("long one", TASK)
    assert hs.cancel(rec["id"]) is True
    await _settle(5)
    assert finished[0]["state"] == "cancelled" and _file(tmp_path)[0]["state"] == "cancelled"
    assert hs.cancel(rec["id"]) is False and hs.cancel("helper-nope") is False
    done = _helpers(tmp_path / "d", Recorder())
    r2 = done.start("quick", TASK); await _settle(5)
    assert done.cancel(r2["id"]) is False


@aio
async def test_cancel_queued_helper_reports_too(tmp_path):
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=finished, max_running=1)
    hs.start("first", TASK); queued = hs.start("second", TASK)
    assert hs.cancel(queued["id"]) is True
    assert finished[0]["id"] == queued["id"] and finished[0]["state"] == "cancelled"


@aio
async def test_mark_lost_on_restart(tmp_path):
    (tmp_path / "helpers.json").write_text(json.dumps({"helpers": [
        {"id": "helper-OLD", "title": "stale job", "task": TASK, "state": "running", "started": 1.0,
         "ended": None, "result": None, "error": None, "reply_via": None},
        {"id": "helper-DONE", "title": "finished", "task": TASK, "state": "done", "started": 1.0,
         "ended": 2.0, "result": "ok", "error": None, "reply_via": None}]}))
    hs = _helpers(tmp_path, Recorder())
    eng = Engine(Store(tmp_path / "c"), Recorder(), lock_path=tmp_path / "turn.lock", helpers=hs)
    await eng.start()
    try:
        assert [r["state"] for r in _file(tmp_path)] == ["lost", "done"]
        users = [e for e in eng.store.history() if e["kind"] == "user"]
        assert len(users) == 1 and users[0]["source"] == "helper"
        assert users[0]["text"] == "[helpers lost on restart] helper-OLD stale job"
    finally:
        await eng.stop()
    clean = _helpers(tmp_path / "clean", Recorder())
    assert clean.mark_lost() == []
    eng2 = Engine(Store(tmp_path / "c2"), Recorder(), lock_path=tmp_path / "turn.lock", helpers=clean)
    await eng2.start()
    try:
        assert [e for e in eng2.store.history() if e["kind"] == "user"] == []
    finally:
        await eng2.stop()


@aio
async def test_stop_marks_helpers_lost_without_finish(tmp_path):
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=finished, max_running=1)
    running = hs.start("running one", TASK); queued = hs.start("queued one", TASK)
    task = hs._tasks[running["id"]]
    hs.shutdown()
    await _settle(5)
    assert task.cancelled() and finished == []
    assert {s["state"] for s in hs.snapshot()} == {"lost"}
    # the file still says running/queued: the restart reports them through mark_lost
    assert [r["state"] for r in _file(tmp_path)] == ["running", "queued"]
    assert [r["id"] for r in H.Helpers(tmp_path / "helpers.json", Recorder(), lambda *a: None,
                                        lambda r: None).mark_lost()] == [running["id"], queued["id"]]


@aio
async def test_snapshot_lists_running_first_with_last_titles(tmp_path):
    clock = {"t": 100.0}
    gate = asyncio.Event()

    async def run(task, helper_id, emit, exact=None):
        for i in range(3):
            emit({"type": "activity", "id": f"t{i}", "tool": "Read", "title": f"Reading f{i}", "state": "running"})
            emit({"type": "activity", "id": f"t{i}", "state": "done"})
        await gate.wait()
        return "ok"
    events = []
    hs = _helpers(tmp_path, run, events=events, clock=lambda: clock["t"], mono=lambda: clock["t"], max_running=1)
    a = hs.start("slow", TASK); b = hs.start("waiting", TASK)
    clock["t"] = 107.0
    await _settle(2)
    snap = hs.snapshot()
    assert [(s["id"], s["state"], s["age"]) for s in snap] == [(a["id"], "running", 7.0), (b["id"], "queued", 7.0)]
    assert snap[0]["last"] == ["Reading f1", "Reading f2"] and snap[1]["last"] == []
    assert events[0] == (a["id"], {"type": "activity", "id": "t0", "tool": "Read", "title": "Reading f0", "state": "running"})
    gate.set(); await _settle(5)
    assert [s["state"] for s in hs.snapshot()] == ["done", "done"]
    assert "no helpers" == await tools.helpers_text(_helpers(tmp_path / "none", run))
    line = (await tools.helpers_text(hs)).splitlines()[0]
    assert line.startswith(f"{a['id']} done ") and "— slow | Reading f1, Reading f2" in line


@aio
async def test_registry_keeps_running_plus_fifty_finished(tmp_path):
    hs = _helpers(tmp_path, Recorder())
    for i in range(60):
        hs.start(f"job {i}", TASK)
    await _settle(100)  # 60 helpers x two file writes: 50 ms was a coin flip on a loaded machine
    recs = _file(tmp_path)
    assert len(recs) == 50 and recs[0]["title"] == "job 10" and recs[-1]["title"] == "job 59"
    assert hs.start("one more", TASK)["state"] == "running"


@aio
async def test_helper_message_with_a_crashing_finish_callback_still_starts_the_next(tmp_path):
    def bad(rec):
        raise RuntimeError("engine gone")
    gate = asyncio.Event()
    hs = _helpers(tmp_path, Recorder(gate=gate), max_running=1)
    hs.on_finish = bad
    hs.start("a", TASK); b = hs.start("b", TASK)
    gate.set(); await _settle(5)
    assert hs.get(b["id"])["state"] == "done"


def test_scratch_only_hook_denies_repo_write():
    def call(tool, path):
        return asyncio.run(H.scratch_only_hook({"tool_name": tool, "tool_input": {"file_path": path},
                                                "cwd": "/Users/Manoj/calwbot"}, None, None))
    for path in ("/Users/Manoj/calwbot/navigator/x.py", "/Users/Manoj/calwbot/tmp_claude/../navigator/x.py",
                 "navigator/x.py", "/Users/Manoj/calwbot/tmp_claude"):
        out = call("Write", path)
        hs = out["hookSpecificOutput"]
        assert hs["hookEventName"] == "PreToolUse" and hs["permissionDecision"] == "deny", path
        assert H.SCRATCH in hs["permissionDecisionReason"]
    assert call("Edit", "/Users/Manoj/calwbot/navigator/x.py")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert call("Write", "/Users/Manoj/calwbot/tmp_claude/w/x.py") == {}
    assert call("Read", "/Users/Manoj/calwbot/navigator/x.py") == {}
    assert call("Bash", "/Users/Manoj/calwbot/navigator/x.py") == {}
    assert asyncio.run(H.scratch_only_hook({"tool_name": "NotebookEdit", "tool_input": {
        "notebook_path": "/Users/Manoj/calwbot/n.ipynb"}}, None, None))["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- sdk_helper: a fresh SDK session per task, the last text block is the report ----

class FakeClient:
    messages: list = []
    seen_options = None
    prompts: list = []

    def __init__(self, options):
        FakeClient.seen_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        FakeClient.prompts.append(prompt)

    async def receive_response(self):
        for m in FakeClient.messages:
            yield m


def _start():
    return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_start", "index": 0,
                                                         "content_block": {"type": "text", "text": ""}})


def _delta(text):
    return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "index": 0,
                                                         "delta": {"type": "text_delta", "text": text}})


def _result(is_error=False, result="ok"):
    return ResultMessage(subtype="success" if not is_error else "error_during_execution", duration_ms=1,
                         duration_api_ms=1, is_error=is_error, num_turns=1, session_id="s-h", result=result)


async def _hook(input_data, tool_use_id, context):
    return {}


def test_helper_result_is_last_text_block(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    monkeypatch.setattr(H, "ClaudeSDKClient", FakeClient)
    FakeClient.prompts = []
    FakeClient.messages = [_start(), _delta("thin"), _delta("king…"),
                           AssistantMessage(content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/a/b.py"})],
                                            model="m", parent_tool_use_id=None),
                           _start(), _delta("REP"), _delta("ORT"), _result()]
    events = []
    out = asyncio.run(H.sdk_helper(TASK, "helper-1", events.append, hook=_hook))
    assert out == "REPORT" and FakeClient.prompts == [TASK]
    assert events[0] == {"type": "ledger_step", "tool": "Read", "file": None, "cmd": None}  # Read is not a write
    assert events[1:-1] == [{"type": "activity", "id": "t1", "tool": "Read", "title": "Reading b.py",
                             "state": "running", "parent": None, "helper": False}]
    assert events[-1]["type"] == "ledger_cost"
    o = FakeClient.seen_options
    assert o.resume is None and o.continue_conversation is False
    assert o.model == H.HELPER_MODEL and set(H.HELPER_TOOLS) <= set(o.tools) and o.max_turns == H.HELPER_MAX_TURNS
    assert "Agent" not in o.tools and "Task" not in o.tools
    assert str(o.cwd) == "/Users/Manoj/calwbot" and o.permission_mode == "auto" and o.can_use_tool is not None
    assert o.system_prompt["type"] == "preset" and o.system_prompt["preset"] == "claude_code"
    assert o.system_prompt["append"].startswith(H.HELPER_PROMPT)
    assert o.hooks["PreToolUse"][0].hooks == [H.scratch_only_hook, _hook]  # the fence runs first


def test_helper_error_result_raises(monkeypatch):
    monkeypatch.setattr(H, "ClaudeSDKClient", FakeClient)
    FakeClient.messages = [_result(is_error=True, result="rate limited")]
    with pytest.raises(RuntimeError, match="rate limited"):
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))


def test_exact_helper_can_only_make_the_approved_call(monkeypatch):
    monkeypatch.setattr(H, "ClaudeSDKClient", FakeClient)
    FakeClient.messages = [_start(), _delta("pushed"), _result()]
    exact = {"tool": "Bash", "input": {"command": "git push origin main"}, "cwd": "/r"}
    assert asyncio.run(H.sdk_helper(TASK, "helper-2", lambda e: None, hook=_hook, exact=exact)) == "pushed"
    o = FakeClient.seen_options
    assert o.tools == ["Bash"] and str(o.cwd) == "/r" and o.resume is None
    fence = o.hooks["PreToolUse"][0].hooks[0]  # the fence runs before the card hook
    same = {"tool_name": "Bash", "tool_input": {"command": "git push origin main", "description": "Push"}, "cwd": "/r"}
    other = {"tool_name": "Bash", "tool_input": {"command": "git push origin dev"}, "cwd": "/r"}
    elsewhere = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": "/other"}
    assert asyncio.run(fence(same, None, None)) == {}
    for call in (other, elsewhere):
        assert asyncio.run(fence(call, None, None))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_conductor_server_exposes_three_tools():
    hs = _helpers(__import__("pathlib").Path("/nonexistent"), Recorder())
    cfg = tools.make_conductor_server(hs)
    assert cfg["type"] == "sdk" and cfg["name"] == "conductor"
    assert tools.TOOL_NAMES == ["delegate", "helpers", "cancel_helper", "restart"]


@aio
async def test_cancel_helper_tool_text(tmp_path):
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    rec = hs.start("long", TASK)
    assert await tools.cancel_text(hs, {"id": rec["id"]}) == f"cancelled {rec['id']}"
    await _settle(3)
    assert await tools.cancel_text(hs, {"id": rec["id"]}) == f"not running: {rec['id']}"


# ---- helpers read ORC project memory (2026-11-07) ----

def _orc_read_tools():
    from navigator.conductor import runner
    return [t for t in runner.MAIN_TOOLS if t.startswith("mcp__orchestrator__")]


def test_helper_session_loads_only_orc_read(monkeypatch):
    """A normal helper loads exactly one MCP server, the orchestrator's (named here, as the main turn
    names it: nothing from the operator's settings files, so alpaca's order tools stay out), with the
    ORC write tools disallowed by name — `tools` does not govern MCP tools — and the ORC read tools
    in `tools` beside the helper's own. Never the conductor server."""
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    from navigator.conductor import runner
    from navigator.conductor.helpers import helper_options
    opts = helper_options(hook=lambda *a: {})
    assert opts.strict_mcp_config is True and set(opts.mcp_servers) == {"orchestrator"}
    assert opts.mcp_servers["orchestrator"] == runner.ORC_MCP
    assert "conductor" not in opts.mcp_servers and "alpaca" not in opts.mcp_servers
    assert opts.disallowed_tools == list(runner.ORC_WRITE_TOOLS)
    assert all(t in opts.tools for t in H.HELPER_TOOLS)
    reads = _orc_read_tools()
    assert reads and all(t in opts.tools for t in reads)
    assert "mcp__orchestrator__get_canonical_memory" in opts.tools
    assert not any(t in opts.tools for t in runner.ORC_WRITE_TOOLS)
    assert not any(t.startswith("mcp__conductor__") or t.startswith("mcp__alpaca") for t in opts.tools)
    assert opts.resume is None


def test_card_helper_still_has_no_mcp(monkeypatch):
    """GUARD (passes on main too): an approved-card helper is unchanged by the ORC read access: no
    MCP server at all, only the one approved tool, today's prompt byte for byte."""
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    from navigator.conductor.helpers import helper_options
    exact = {"tool": "Bash", "input": {"command": "git push origin main"}, "cwd": "/r"}
    opts = helper_options(lambda *a: {}, exact=exact)
    assert opts.strict_mcp_config is True and opts.mcp_servers == {}
    assert opts.tools == ["Bash"] and opts.disallowed_tools == []
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": H.HELPER_PROMPT}
    assert "get_canonical_memory" not in opts.system_prompt["append"]


def test_helper_orc_switch_off_restores_old_options(monkeypatch):
    """GUARD (passes on main too): CONDUCTOR_HELPER_ORC="0" is today's helper, byte for byte: no MCP
    server, the eight helper tools only, nothing disallowed, today's prompt."""
    monkeypatch.setenv("CONDUCTOR_HELPER_ORC", "0")
    from navigator.conductor.helpers import helper_options
    opts = helper_options(hook=lambda *a: {})
    assert opts.strict_mcp_config is True and opts.mcp_servers == {}
    assert opts.tools == list(H.HELPER_TOOLS)
    assert opts.disallowed_tools == []  # the SDK default: today passes none
    assert not any(t.startswith("mcp__orchestrator__") for t in opts.disallowed_tools)
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": H.HELPER_PROMPT}
    assert "get_canonical_memory" not in opts.system_prompt["append"]


def test_helper_prompt_mentions_orc_read_only(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    from navigator.conductor.helpers import helper_options
    append = helper_options(hook=lambda *a: {}).system_prompt["append"]
    assert append.startswith(H.HELPER_PROMPT)
    assert "get_canonical_memory" in append and "mcp__orchestrator__search" in append
    assert "Never call ORC write" in append
    assert "If they are absent, continue without them." in append


def test_helper_session_loads_no_plugin_injection():
    from navigator.conductor import runner
    from navigator.conductor.helpers import helper_options
    for o in (helper_options(hook=lambda *a: {}),
              helper_options(lambda *a: {}, exact={"tool": "Bash", "input": {"command": "x"}, "cwd": "/tmp"})):
        s = json.loads(o.settings)
        assert set(s["enabledPlugins"]) == set(runner._PLUGINS_OFF)
        assert all(v is False for v in s["enabledPlugins"].values())
        assert s["autoMemoryEnabled"] is False
        assert o.setting_sources == ["user", "project", "local"]


def test_helper_gate_hooks_intact():
    from navigator.conductor import gate
    from navigator.conductor.helpers import helper_options
    o = helper_options(hook=lambda *a: {})
    hooks = o.hooks["PreToolUse"][0].hooks
    assert len(hooks) == 2 and all(callable(h) for h in hooks)
    assert o.can_use_tool is gate.can_use_tool


# ---- per-delegate timeout_s, one long helper at a time (2026-11-07) ----

def _fast(monkeypatch):
    monkeypatch.setattr(H, "HELPER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(H, "HELPER_MIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(H, "HELPER_MAX_TIMEOUT_S", 0.3)


class Sleeper:
    def __init__(self, secs):
        self.secs = secs

    async def __call__(self, task, helper_id, emit, exact=None):
        await asyncio.sleep(self.secs)
        return "ok"


@aio
async def test_timeout_s_per_delegate(tmp_path, monkeypatch):
    _fast(monkeypatch)
    fin = []
    hs = _helpers(tmp_path, Sleeper(0.1), finished=fin)
    hs.start("a", TASK, timeout_s=0.2); await asyncio.sleep(0.3)
    assert fin[0]["state"] == "done"
    hs = _helpers(tmp_path / "b", Sleeper(1), finished=fin)
    hs.start("b", TASK, timeout_s=0.1); await asyncio.sleep(0.3)
    assert "timed out after" in fin[1]["error"] and "delegate again" in fin[1]["error"]
    hs = _helpers(tmp_path / "c", Sleeper(1), finished=fin)
    hs.start("c", TASK, timeout_s=5); await asyncio.sleep(0.5)
    assert "timed out after" in fin[2]["error"] and "delegate again" not in fin[2]["error"]


@aio
async def test_timeout_s_clamped(tmp_path):
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    r = await tools.delegate_text(hs, {"title": "t", "task": TASK, "timeout_s": 99999})
    assert "capped at 1800s" in r and _file(tmp_path)[0]["timeout_s"] == 1800.0
    r = await tools.delegate_text(hs, {"title": "t2", "task": TASK, "timeout_s": "5"})
    assert "raised to 60s" in r and _file(tmp_path)[1]["timeout_s"] == 60.0
    n = len(hs.snapshot())
    for bad in ("abc", True, float("nan"), float("inf"), "nan"):
        r = await tools.delegate_text(hs, {"title": "t3", "task": TASK, "timeout_s": bad})
        assert r.startswith("refused"), bad
    assert len(hs.snapshot()) == n
    r = await tools.delegate_text(hs, {"title": "t4", "task": TASK, "timeout_s": "1200"})
    assert r.startswith("started") and _file(tmp_path)[2]["timeout_s"] == 1200.0


@aio
async def test_exact_helper_ignores_timeout_s(tmp_path):
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    hs.start("e", TASK, exact={"x": 1}, timeout_s=1800)
    assert _file(tmp_path)[0]["timeout_s"] is None


@aio
async def test_old_record_without_timeout_runs_on_default(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "HELPER_TIMEOUT_S", 0.05)
    fin = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=fin)
    rec = hs.start("o", TASK)
    for r in hs._records:
        r.pop("timeout_s", None)
    await asyncio.sleep(0.2)
    assert fin and "timed out" in fin[0]["error"]


@aio
async def test_only_one_long_helper_at_a_time(tmp_path):
    gates = {}

    async def run(task, hid, emit, exact=None):
        gates[hid] = ev = asyncio.Event()
        await ev.wait()
        return "ok"

    hs = _helpers(tmp_path, run)
    A = hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    C = hs.start("C", TASK)
    await _settle(10)
    st = lambda r: next(x["state"] for x in hs._records if x["id"] == r["id"])
    assert (st(A), st(B), st(C)) == ("running", "queued", "running")
    D = hs.start("D", TASK, timeout_s=900)
    E = hs.start("E", TASK, timeout_s=600)  # exactly 600 is not long
    await _settle(10)
    assert st(D) == "queued" and st(E) == "running"
    assert hs.cancel(D["id"]) and st(D) == "cancelled"
    gates[A["id"]].set(); await _settle(10)
    assert st(B) == "running"
    r = await tools.delegate_text(hs, {"title": "L", "task": TASK, "timeout_s": 900})
    assert "only one long helper" in r
    X = hs.start("X", TASK, exact={"k": 1}, timeout_s=900)
    assert next(x for x in hs._records if x["id"] == X["id"])["timeout_s"] is None


# ---- queue / watchdog fixes (2026-11-08) ----

def _rec(hs, r):
    return next(x for x in hs._records if x["id"] == r["id"])


def _snap(hs, r):
    return next(x for x in hs.snapshot() if x["id"] == r["id"])


@aio
async def test_snapshot_age_counts_from_launch_not_queue(tmp_path):
    now = [1000.0]
    gate = asyncio.Event()
    hs = _helpers(tmp_path, Recorder(gate=gate), clock=lambda: now[0])
    hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    assert _rec(hs, B)["state"] == "queued"
    now[0] += 1000
    await _settle(3)  # A's task must start with the old runner
    hs.runner = Recorder(gate=asyncio.Event())  # B's own run waits on a fresh gate
    gate.set(); await _settle(10)
    assert _rec(hs, B)["state"] == "running"
    s = _snap(hs, B)
    assert s["age"] < 5 and abs(s["queued_s"] - 1000) < 5
    hs.shutdown(); await _settle(5)


@aio
async def test_helpers_text_shows_queue_wait(tmp_path):
    now = [1000.0]
    gate = asyncio.Event()
    hs = _helpers(tmp_path, Recorder(gate=gate), clock=lambda: now[0])
    hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    now[0] += 1000
    txt = await tools.helpers_text(hs)
    assert f"{B['id']} queued 1000s" in txt
    await _settle(3)  # A's task must start with the old runner
    hs.runner = Recorder(gate=asyncio.Event())
    gate.set(); await _settle(10)
    txt = await tools.helpers_text(hs)
    assert f"{B['id']} running 0s (queued 1000s)" in txt
    hs.shutdown(); await _settle(5)


@aio
async def test_end_save_failure_still_starts_queue(tmp_path):
    gate = asyncio.Event()
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=gate), finished=finished)
    A = hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    orig, calls = hs._save, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        orig()
    hs._save = flaky
    await _settle(3)  # A's task must start with the old runner
    hs.runner = Recorder(gate=asyncio.Event())
    gate.set(); await _settle(10)
    assert [f["id"] for f in finished][:1] == [A["id"]]
    assert _rec(hs, B)["state"] == "running"
    hs.shutdown(); await _settle(5)


@aio
async def test_timeout_fires_even_if_runner_swallows_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "CANCEL_GRACE_S", 0.05, raising=False)
    monkeypatch.setattr(H, "HELPER_MIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(H, "LONG_S", 0.01)

    async def stubborn(task, helper_id, emit, exact=None):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(60)
    hs = _helpers(tmp_path, stubborn)
    A = hs.start("A", TASK, timeout_s=0.05)
    B = hs.start("B", TASK, timeout_s=0.05)
    assert _rec(hs, B)["state"] == "queued"
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if _rec(hs, A)["state"] == "failed":
                break
        assert _rec(hs, A)["state"] == "failed" and "timed out" in _rec(hs, A)["error"]
        assert _rec(hs, B).get("launched")
    finally:
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.sleep(0.05)


@aio
async def test_queued_helper_expires(tmp_path):
    now = [1000.0]
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=finished, clock=lambda: now[0])
    A = hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    now[0] += H.QUEUE_MAX_S + 1
    hs.expire_queued()
    r = _rec(hs, B)
    assert r["state"] == "failed" and "in the queue" in r["error"] and "waited" in r["error"]
    assert [f["id"] for f in finished] == [B["id"]]
    assert _rec(hs, A)["state"] == "running"
    hs.shutdown(); await _settle(5)


@aio
async def test_no_stale_long_count_after_end(tmp_path):
    gate = asyncio.Event()
    hs = _helpers(tmp_path, Recorder(gate=gate))
    hs.start("A", TASK, timeout_s=900)
    B = hs.start("B", TASK, timeout_s=900)
    await _settle(3)  # A's task must start with the old runner
    hs.runner = Recorder(gate=asyncio.Event())
    gate.set(); await _settle(10)
    assert hs._running_long() == 1 and _rec(hs, B)["state"] == "running"
    hs.shutdown(); await _settle(5)


@aio
async def test_tick_uses_monotonic_for_run_not_wall(tmp_path):
    now, m = [1000.0], [50.0]
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), clock=lambda: now[0], mono=lambda: m[0])
    A = hs.start("A", TASK, timeout_s=900)
    now[0] += 36000  # the Mac slept
    hs.tick()
    assert _rec(hs, A)["state"] == "running" and _snap(hs, A)["age"] < 5
    m[0] += 900 + H.CANCEL_GRACE_S + H.TICK_S + 1
    hs.tick()
    r = _rec(hs, A)
    assert r["state"] == "failed" and "timed out" in r["error"]
    await _settle(5)


@aio
async def test_end_is_idempotent(tmp_path):
    finished = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()), finished=finished)
    A = hs.start("A", TASK)
    r = _rec(hs, A)
    hs._end(r, "failed", error="x")
    hs._end(r, "cancelled")
    assert len(finished) == 1 and r["state"] == "failed"
    hs.shutdown(); await _settle(5)


@aio
async def test_shutdown_cancels_inner_runner_task(tmp_path):
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    hs.start("A", TASK)
    await _settle(3)
    hs.shutdown(); await _settle(5)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


# ---- reaping a dead helper's tagged children (2026-11-08) ----

def _alive(p):
    return p.poll() is None


_PROCS = []


def _spawn(**kw):
    import subprocess
    p = subprocess.Popen(["sleep", "300"], **kw)
    _PROCS.append(p)
    return p


@pytest.fixture(autouse=True)
def _cleanup_procs():
    yield
    while _PROCS:
        p = _PROCS.pop()
        p.kill(); p.wait()


def _piddir(tmp_path, *pids, name="job"):
    d = tmp_path / "pids"
    d.mkdir(exist_ok=True)
    for i, pid in enumerate(pids):
        (d / f"{name}{i}.pid").write_text(f"{pid}\n")
    return d


def test_reap_spares_older_same_uid_process(tmp_path):
    old = _spawn()  # a recycled pid owned by the same user: started BEFORE the helper launched
    time.sleep(3.2)
    launched = time.time()
    d = _piddir(tmp_path, old.pid)
    assert H.reap(d, launched) == 0
    assert _alive(old)
    assert not d.exists()


def test_reap_kills_process_started_after_launch(tmp_path):
    launched = time.time()
    time.sleep(1.1)
    new = _spawn()
    d = _piddir(tmp_path, new.pid)
    assert H.reap(d, launched) == 1
    assert new.wait(timeout=5) != 0


def test_reap_skips_when_start_time_unreadable(tmp_path, monkeypatch):
    p = _spawn()
    d = _piddir(tmp_path, p.pid)
    monkeypatch.setattr(H, "_start_time", lambda pid: None)
    assert H.reap(d, time.time() - 5) == 0
    assert _alive(p)


def test_reap_kills_group_leader_and_removes_dir(tmp_path):
    import time
    p = _spawn(start_new_session=True)  # its own group leader, like `perl -e setpgrp`
    kid = _spawn(start_new_session=False)
    d = _piddir(tmp_path, p.pid)
    time.sleep(0.2)
    assert H.reap(d, time.time() - 5) == 1
    assert p.wait(timeout=5) != 0
    assert _alive(kid)  # not listed: untouched
    assert not d.exists()


def test_reap_kills_plain_pid(tmp_path):
    p = _spawn()  # same process group as the tests: only the pid may be signalled
    d = _piddir(tmp_path, p.pid)
    assert H.reap(d, time.time() - 5) == 1
    assert p.wait(timeout=5) != 0
    assert not d.exists()


def test_reap_skips_other_uid(tmp_path, monkeypatch):
    import os
    p = _spawn()
    d = _piddir(tmp_path, p.pid)
    monkeypatch.setattr(H, "_pid_info", lambda pid: (os.getuid() + 1, "S"))
    assert H.reap(d, time.time() - 5) == 0
    assert _alive(p)


def test_reap_skips_self_and_ancestors(tmp_path, monkeypatch):
    import os
    sent = []
    monkeypatch.setattr(H.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(H.os, "killpg", lambda pg, sig: sent.append(-pg))
    d = _piddir(tmp_path, os.getpid(), os.getppid(), 1, 0, -5, "junk")
    assert H.reap(d, time.time() - 5) == 0
    assert sent == []


def test_reap_stale_pid_is_fine(tmp_path):
    p = _spawn()
    p.kill(); p.wait()
    d = _piddir(tmp_path, p.pid)
    assert H.reap(d, time.time() - 5) == 0
    assert not d.exists()
    assert H.reap(tmp_path / "never-made", time.time()) == 0


@aio
async def test_success_leaves_processes_and_removes_dir(tmp_path):
    p = _spawn()
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    A = hs.start("A", TASK)
    await _settle(3)
    d = hs._piddir(A["id"])
    d.mkdir(parents=True)
    (d / "j.pid").write_text(str(p.pid))
    hs._end(_rec(hs, A), "done"); await _settle(5)
    assert _alive(p) and not d.exists()
    hs.shutdown(); await _settle(3)


@aio
async def test_end_failure_reaps_real_process(tmp_path):
    p = _spawn()
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    A = hs.start("A", TASK)
    await _settle(3)
    d = hs._piddir(A["id"])
    d.mkdir(parents=True)
    (d / "j.pid").write_text(str(p.pid))
    hs._end(_rec(hs, A), "cancelled")
    assert p.wait(timeout=10) != 0
    hs.shutdown(); await _settle(3)



@aio
async def test_end_reap_policy(tmp_path, monkeypatch):
    # cancelled and failed (timed out) launched helpers reap once; done / exact / never-launched do not
    for state in ("cancelled", "failed", "done"):
        calls = []
        hs = _helpers(tmp_path / state, Recorder(gate=asyncio.Event()))
        hs._reap = calls.append
        A = hs.start("A", TASK)
        await _settle(3)
        r = _rec(hs, A)
        hs._end(r, state, **({"error": "timed out"} if state == "failed" else {}))
        await _settle(5)
        assert calls == ([A["id"]] if state != "done" else []), state
        hs._end(r, "failed")  # idempotent: no second reap
        await _settle(5)
        assert len(calls) == (0 if state == "done" else 1)
        hs.shutdown(); await _settle(3)
    calls = []
    hs = _helpers(tmp_path / "exact", Recorder(gate=asyncio.Event()))
    hs._reap = calls.append
    X = hs.start("X", TASK, exact={"tool": "Bash", "input": {}, "cwd": "/"})
    await _settle(3)
    hs._end(_rec(hs, X), "cancelled"); await _settle(5)
    assert calls == []
    hs.shutdown(); await _settle(3)
    # queued then cancelled: nothing ran
    calls = []
    hs = _helpers(tmp_path / "queued", Recorder(gate=asyncio.Event()))
    hs._reap = calls.append
    hs.start("L1", TASK, timeout_s=900)
    Q = hs.start("L2", TASK, timeout_s=900)
    assert hs.cancel(Q["id"]); await _settle(5)
    assert calls == []
    hs.shutdown(); await _settle(3)
    # a raising reap never stops the report or the queue
    finished = []
    hs = _helpers(tmp_path / "raises", Recorder(gate=asyncio.Event()), finished=finished)

    def boom(hid):
        raise RuntimeError("boom")
    hs._reap = boom
    L1 = hs.start("L1", TASK, timeout_s=900)
    L2 = hs.start("L2", TASK, timeout_s=900)
    await _settle(3)
    hs._end(_rec(hs, L1), "cancelled"); await _settle(5)
    assert [f["id"] for f in finished] == [L1["id"]] and _rec(hs, L2)["state"] == "running"
    hs.shutdown(); await _settle(3)


@aio
async def test_reap_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HELPER_REAP", "0")
    calls = []
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    hs._reap = calls.append
    A = hs.start("A", TASK)
    await _settle(3)
    hs._end(_rec(hs, A), "cancelled"); await _settle(5)
    assert calls == []
    hs.shutdown(); await _settle(3)


def test_helper_options_sets_piddir_env(tmp_path):
    import os
    assert not H.helper_options(None).env
    assert not H.helper_options(None, helper_id="helper-X").env  # no root given
    o = H.helper_options(None, helper_id="helper-X", pid_root=tmp_path / "helper-pids")
    d = tmp_path / "helper-pids" / "helper-X"
    assert o.env == {"NAV_HELPER_PIDDIR": str(d)} and d.is_dir() and (d.stat().st_mode & 0o777) == 0o700
    exact = {"tool": "Bash", "input": {"command": "ls"}, "cwd": "/Users/Manoj/calwbot"}
    assert not H.helper_options(None, exact=exact, helper_id="helper-Y", pid_root=tmp_path / "hp2").env
    assert not (tmp_path / "hp2").exists()


def test_prompt_asks_for_pidfile():
    assert "NAV_HELPER_PIDDIR" in H.HELPER_PROMPT and "setpgrp" in H.HELPER_PROMPT


def test_prompt_is_portable_on_macos():
    import re
    P = H.HELPER_PROMPT
    assert "nohup" in P and "alarm shift" in P and "1500" in P and "DONE" in P
    assert re.search(r"(?<![\w-])g?timeout\b", P) is None


def test_prompt_pattern_really_times_out():
    import os
    import subprocess
    import time
    if not os.path.exists("/usr/bin/perl"):
        pytest.skip("no perl")
    t = time.monotonic()
    r = subprocess.run(["/usr/bin/perl", "-e", "alarm shift; exec @ARGV", "1", "sleep", "30"], timeout=10)
    assert time.monotonic() - t < 5 and r.returncode != 0


# ---- credits fallback (2026-11-08): route before launch, one retry on a limit only if nothing ran ----

from navigator import credits as C  # noqa: E402

LIMIT_TEXT = "You've hit your limit · resets 3pm (America/Los_Angeles)"
KEY = "sk-ant-SENTINELKEY123456"
CARD_CMD = "make deploy-site"


class AttemptClient:
    attempts: list = []
    opts: list = []
    n = 0

    def __init__(self, options):
        AttemptClient.opts.append(options)
        self.i = AttemptClient.n
        AttemptClient.n += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        for m in AttemptClient.attempts[self.i]:
            if isinstance(m, Exception):
                raise m
            yield m


class Fake:
    def __init__(self, routes):
        self.routes, self.calls = list(routes), []
        self.released, self.recorded, self.limits = [], [], []

    def install(self, monkeypatch, attempts):
        AttemptClient.attempts, AttemptClient.opts, AttemptClient.n = attempts, [], 0
        monkeypatch.setattr(H, "ClaudeSDKClient", AttemptClient)
        monkeypatch.setattr(C, "route", lambda *a, **k: self.routes.pop(0) if self.routes else C.Route())
        monkeypatch.setattr(C, "on_limit", lambda lim, *a, **k: (self.limits.append(lim), self.routes.pop(0) if self.routes else None)[1])
        monkeypatch.setattr(C, "record", lambda cost, res, **k: self.recorded.append((cost, res)))
        monkeypatch.setattr(C, "release", lambda res, *a, **k: self.released.append(res))
        monkeypatch.setattr(C, "load_key", lambda: KEY)


def _limit_result():
    return _result(is_error=True, result=LIMIT_TEXT)


def _asst(*blocks):
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=None)


def _creds_route(res="r1", budget=2.0):
    return C.Route({"ANTHROPIC_API_KEY": KEY}, None, C.Reservation(res, budget, 0.0), budget)


def test_limit_before_anything_retries_once_with_credits_env(monkeypatch):
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[_limit_result()], [_start(), _delta("fine"), _result()]])
    assert asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook)) == "fine"
    assert len(AttemptClient.opts) == 2 and len(f.limits) == 1
    assert "ANTHROPIC_API_KEY" not in (AttemptClient.opts[0].env or {})
    assert AttemptClient.opts[1].env["ANTHROPIC_API_KEY"] == KEY
    assert [r.id for _, r in f.recorded] == ["r1"]  # the subscription attempt records nothing


def test_route_before_launch_uses_credits_env_first(monkeypatch):
    f = Fake([_creds_route()])
    f.install(monkeypatch, [[_start(), _delta("ok"), _result()]])
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert len(AttemptClient.opts) == 1 and AttemptClient.opts[0].env["ANTHROPIC_API_KEY"] == KEY and not f.limits


def test_limit_after_text_or_tool_is_not_retried(monkeypatch):
    for first in ([_start(), _delta("partial")],
                  [_asst(ToolUseBlock(id="t", name="Read", input={"file_path": "/a"}))]):
        f = Fake([C.Route(), _creds_route()])
        f.install(monkeypatch, [first + [_limit_result()], [_result()]])
        with pytest.raises(RuntimeError) as ei:
            asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
        assert len(AttemptClient.opts) == 1 and not f.limits
        assert "limit" in str(ei.value) and KEY not in str(ei.value)


def test_limit_twice_ends_without_third_attempt(monkeypatch):
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[_limit_result()], [_limit_result()], [_result()]])
    with pytest.raises(RuntimeError):
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert len(AttemptClient.opts) == 2 and len(f.limits) == 1


def test_no_credits_route_ends_with_note(monkeypatch):
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_limit_result()], [_result()]])
    with pytest.raises(RuntimeError, match="limit"):
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert len(AttemptClient.opts) == 1


def test_rate_limit_event_alone_then_retry(monkeypatch):
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo
    ev = RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=None, rate_limit_type="five_hour",
                                                      utilization=1.0, overage_status=None, overage_resets_at=None,
                                                      overage_disabled_reason=None, raw={}),
                        uuid="u", session_id="s")
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[ev, _result(is_error=True, result="x")], [_start(), _delta("go"), _result()]])
    assert asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook)) == "go"
    assert len(f.limits) == 1


def test_exact_helper_retries_only_before_its_call(monkeypatch):
    exact = {"tool": "Bash", "input": {"command": CARD_CMD}, "cwd": "/r"}
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[_limit_result()], [_start(), _delta("done"), _result()]])
    assert asyncio.run(H.sdk_helper(TASK, "helper-2", lambda e: None, hook=_hook, exact=exact)) == "done"
    assert len(AttemptClient.opts) == 2 and AttemptClient.opts[1].tools == ["Bash"]
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[_asst(ToolUseBlock(id="t", name="Bash", input={"command": CARD_CMD})),
                             _limit_result()], [_result()]])
    with pytest.raises(RuntimeError):
        asyncio.run(H.sdk_helper(TASK, "helper-2", lambda e: None, hook=_hook, exact=exact))
    assert len(AttemptClient.opts) == 1


def test_reservation_released_on_crash_and_recorded_on_result(monkeypatch):
    res = C.Reservation("r9", 2.0, 0.0)
    f = Fake([C.Route({"ANTHROPIC_API_KEY": KEY}, None, res)])
    f.install(monkeypatch, [[ValueError("boom " + KEY)]])
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    # launched, then crashed with no result: the hold is charged (record None cost), never freed
    assert KEY not in str(ei.value) and f.released == [] and f.recorded == [(None, res)]
    f = Fake([C.Route({"ANTHROPIC_API_KEY": KEY}, None, res)])
    f.install(monkeypatch, [[_start(), _delta("x"), _result()]])
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert [r for _, r in f.recorded] == [res] and f.released in ([], [res])


def test_results_and_error_results_are_redacted(monkeypatch):
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_start(), _delta("key is " + KEY), _result()]])
    out = asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert KEY not in out and "[redacted]" in out
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_result(is_error=True, result="bad " + KEY)]])
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert KEY not in str(ei.value)


def test_options_equal_apart_from_env_and_piddir_kept(tmp_path, monkeypatch):
    import dataclasses
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    base = H.helper_options(_hook, None, "helper-Z", tmp_path)
    withc = H.helper_options(_hook, None, "helper-Z", tmp_path, extra_env={"ANTHROPIC_API_KEY": KEY})
    assert withc.env == {**base.env, "ANTHROPIC_API_KEY": KEY} and "NAV_HELPER_PIDDIR" in withc.env
    names = [x.name for x in dataclasses.fields(base) if x.name != "env"]
    assert [getattr(base, n) for n in names] == [getattr(withc, n) for n in names]
    f = Fake([_creds_route()])
    f.install(monkeypatch, [[_result()]])
    asyncio.run(H.sdk_helper(TASK, "helper-Z", lambda e: None, hook=_hook, pid_root=tmp_path))
    e = AttemptClient.opts[0].env
    assert e["ANTHROPIC_API_KEY"] == KEY and e["NAV_HELPER_PIDDIR"].endswith("helper-Z")


# ---- UNVERIFIED rule (2026-11-09)
UNV = "For every claim you did not verify with a tool in this task, prefix the line with UNVERIFIED:"


def test_helper_prompt_has_unverified_rule():
    assert UNV in H.HELPER_PROMPT
    assert H.HELPER_PROMPT.index(UNV) < H.HELPER_PROMPT.index("Keep the closing report")


def test_unverified_rule_reaches_helper_options(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HELPER_ORC", "0")
    opts = H.helper_options(hook=lambda *a: {})
    assert UNV in opts.system_prompt["append"] and opts.system_prompt["append"] == H.HELPER_PROMPT
    exact = {"tool": "Bash", "input": {"command": "git push origin main"}, "cwd": "/r"}
    assert UNV in H.helper_options(lambda *a: {}, exact=exact).system_prompt["append"]


@aio
async def test_report_with_probably_is_untouched(tmp_path):
    text = "tests should pass, probably fine"
    hs = _helpers(tmp_path, Recorder(result=text))
    hs.start("a", TASK)
    await _settle(10)
    (rec,) = _file(tmp_path)
    assert rec["state"] == "done" and rec["result"] == text


@aio
async def test_unverified_line_kept_verbatim_across_cut(tmp_path):
    text = "UNVERIFIED: the port is 8080\n" + "y" * (H.RESULT_CHARS + 50)
    hs = _helpers(tmp_path, Recorder(result=text))
    hs.start("a", TASK)
    await _settle(10)
    (rec,) = _file(tmp_path)
    assert rec["result"].startswith("UNVERIFIED: the port is 8080\n")


# ---- credits follow-ups (2026-11-09) ----

def test_rate_limit_assistant_message_does_not_count_as_started(monkeypatch):
    from claude_agent_sdk import TextBlock
    rl = AssistantMessage(content=[TextBlock(text=LIMIT_TEXT)], model="m", parent_tool_use_id=None, error="rate_limit")
    f = Fake([C.Route(), _creds_route()])
    f.install(monkeypatch, [[rl, _limit_result()], [_start(), _delta("fine"), _result()]])
    assert asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook)) == "fine"
    assert len(AttemptClient.opts) == 2


def test_limit_while_already_on_credits_is_not_retried(monkeypatch):
    f = Fake([_creds_route(), _creds_route("r2")])
    f.install(monkeypatch, [[_limit_result()], [_result()]])
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert len(AttemptClient.opts) == 1 and not f.limits and KEY not in str(ei.value)


def test_credit_balance_error_on_credits_call_marks_exhausted(monkeypatch):
    f = Fake([_creds_route()])
    f.install(monkeypatch, [[_result(is_error=True, result="Credit balance is too low")]])
    marked = []
    monkeypatch.setattr(C, "mark_exhausted", lambda *a, **k: marked.append(1))
    with pytest.raises(RuntimeError):
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert marked == [1]


# ---- credits ledger guard (2026-11-09): only calls that ran on the key are recorded ----

def test_subscription_helper_records_no_credits_spend(monkeypatch):
    """A helper on the subscription (plain Route, no reservation) must not touch the credits
    ledger: the live ledger showed 54 flat $2.20 entries written by subscription helpers."""
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_start(), _delta("fine"), _result()]])
    assert asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook)) == "fine"
    assert f.recorded == []
    assert f.released == []


def test_card_helper_on_subscription_records_no_credits_spend(monkeypatch):
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_start(), _delta("ran"), _result()]])
    exact = {"tool": "Bash", "input": {"command": CARD_CMD}, "cwd": "/r"}
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook, exact=exact))
    assert f.recorded == []


def test_credits_attempt_carries_its_budget_and_subscription_attempt_none(monkeypatch):
    f = Fake([C.Route(), _creds_route(budget=3.5)])
    f.install(monkeypatch, [[_limit_result()], [_start(), _delta("fine"), _result()]])
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert [o.max_budget_usd for o in AttemptClient.opts] == [None, 3.5]
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_start(), _delta("ran"), _result()]])
    exact = {"tool": "Bash", "input": {"command": CARD_CMD}, "cwd": "/r"}
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook, exact=exact))
    assert AttemptClient.opts[0].max_budget_usd is None


def test_route_is_called_off_the_loop(monkeypatch):
    import threading
    idents = []
    f = Fake([C.Route()])
    f.install(monkeypatch, [[_start(), _delta("fine"), _result()]])
    inner = C.route
    monkeypatch.setattr(C, "route", lambda *a, **k: (idents.append(threading.get_ident()), inner(*a, **k))[1])
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert idents and idents[0] != threading.main_thread().ident


def test_approved_card_helper_on_the_key_carries_its_budget(monkeypatch):
    f = Fake([_creds_route(budget=1.25)])
    f.install(monkeypatch, [[_start(), _delta("ran"), _result()]])
    exact = {"tool": "Bash", "input": {"command": CARD_CMD}, "cwd": "/r"}
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook, exact=exact))
    assert AttemptClient.opts[0].max_budget_usd == 1.25 and AttemptClient.opts[0].env["ANTHROPIC_API_KEY"] == KEY


def test_budget_stop_is_explained(monkeypatch):
    f = Fake([_creds_route()])
    f.install(monkeypatch, [[_start(), _result(is_error=True, result=None)]])
    AttemptClient.attempts[0][-1].subtype = "error_max_budget_usd"
    with pytest.raises(RuntimeError, match="max_budget"):
        asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))


def test_credits_helper_records_exactly_its_reservation(monkeypatch):
    f = Fake([_creds_route()])
    f.install(monkeypatch, [[_start(), _delta("ok"), _result()]])
    asyncio.run(H.sdk_helper(TASK, "helper-1", lambda e: None, hook=_hook))
    assert [r.id for _, r in f.recorded] == ["r1"]


# ---- completion ledger (2026-11-09): every ledger sink here is a list; nothing reaches the real file ----

def _ledgered(tmp_path, runner, sink, **kw):
    seen = kw.pop("seen", None)
    return _helpers(tmp_path, runner, ledger=sink.append,
                    ledger_seen=(lambda: {r["id"] for r in sink}) if seen is None else seen, **kw)


class Stepper:
    def __init__(self, result="done\nUNVERIFIED: a claim\n", raise_exc=None, gate=None):
        self.result, self.raise_exc, self.gate = result, raise_exc, gate

    async def __call__(self, task, helper_id, emit, exact=None):
        emit({"type": "ledger_step", "tool": "Read", "file": None, "cmd": None})
        emit({"type": "activity", "id": "1", "tool": "Read", "title": "Read x", "state": "running"})
        emit({"type": "ledger_step", "tool": "Edit", "file": "/w/a.py", "cmd": None})
        emit({"type": "ledger_step", "tool": "Write", "file": "/w/b.py", "cmd": None})
        emit({"type": "ledger_step", "tool": "Bash", "file": None, "cmd": "pytest -q tests/"})
        emit({"type": "ledger_cost", "usd": 0.25})
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_exc:
            raise self.raise_exc
        return self.result


@aio
async def test_done_helper_writes_one_ledger_record(tmp_path):
    sink = []
    hs = _ledgered(tmp_path, Stepper(), sink)
    rec = hs.start("t", TASK)
    await _settle(20)
    assert len(sink) == 1
    r = sink[0]
    assert r["id"] == rec["id"] and r["kind"] == "helper" and r["state"] == "done" and r["v"] == 1
    assert r["tools"] == {"Read": 1, "Edit": 1, "Write": 1, "Bash": 1}
    assert r["files"] == ["/w/a.py", "/w/b.py"] and r["commands"] == ["pytest -q tests/"]
    assert r["cost_usd"] == 0.25 and r["unverified"] == 1
    assert "task" not in r and "result" not in r


@aio
async def test_failed_cancelled_timeout_each_write_one_record(tmp_path, monkeypatch):
    sink = []
    hs = _ledgered(tmp_path, Stepper(raise_exc=RuntimeError("boom")), sink)
    hs.start("f", TASK)
    await _settle(20)
    gate = asyncio.Event()
    hs2 = _ledgered(tmp_path / "b", Stepper(gate=gate), sink)
    r2 = hs2.start("c", TASK)
    await _settle(5)
    hs2.cancel(r2["id"])
    await _settle(20)
    monkeypatch.setattr(H, "HELPER_TIMEOUT_S", 0.05)
    hs3 = _ledgered(tmp_path / "c", Stepper(gate=asyncio.Event()), sink)
    hs3.start("t", TASK)
    await _settle(60)
    assert sorted(r["state"] for r in sink) == ["cancelled", "failed", "failed"]
    n = len(sink)
    for h in (hs, hs2, hs3):
        for rec in list(h._records):
            h._end(rec, "failed")  # a second end writes nothing
    assert len(sink) == n


@aio
async def test_ledger_events_never_reach_on_event(tmp_path):
    sink, events = [], []
    hs = _ledgered(tmp_path, Stepper(), sink, events=events)
    hs.start("t", TASK)
    await _settle(20)
    assert events and all(ev["type"] == "activity" for _h, ev in events)


@aio
async def test_ledger_failure_does_not_stop_finish_or_queue(tmp_path):
    finished = []

    def boom(rec):
        raise OSError("disk")
    hs = _helpers(tmp_path, Stepper(), finished, max_running=1, ledger=boom, ledger_seen=lambda: set())
    hs.start("a", TASK)
    hs.start("b", TASK)
    await _settle(60)
    assert [r["state"] for r in finished] == ["done", "done"]


@aio
async def test_mark_lost_and_shutdown_write_lost_once(tmp_path):
    sink = []
    gate = asyncio.Event()
    hs = _ledgered(tmp_path, Stepper(gate=gate), sink)
    a = hs.start("a", TASK)
    await _settle(5)
    hs.shutdown()  # in memory only; the file still says running
    await _settle(5)
    assert [(r["id"], r["state"]) for r in sink] == [(a["id"], "lost")]
    assert sink[0]["tools"]["Edit"] == 1
    hs2 = _ledgered(tmp_path, Recorder(), sink)  # restart: the id already has a line
    assert [r["id"] for r in hs2.mark_lost()] == [a["id"]]
    assert len(sink) == 1
    # a helper killed with no line at all gets one, once
    (tmp_path / "helpers.json").write_text(json.dumps({"helpers": [
        {"id": "helper-zz", "title": "z", "state": "running", "started": 1.0}]}))
    hs3 = _ledgered(tmp_path, Recorder(), sink)
    hs3.mark_lost()
    hs3.mark_lost()
    assert [r["id"] for r in sink].count("helper-zz") == 1 and sink[-1]["state"] == "lost"
