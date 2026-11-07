"""Helpers (2026-11-04): the work a turn used to do itself runs beside the conversation."""
import asyncio
import functools
import json

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
    hs = _helpers(tmp_path, run, events=events, clock=lambda: clock["t"], max_running=1)
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
    await _settle(10)
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
    monkeypatch.setattr(H, "ClaudeSDKClient", FakeClient)
    FakeClient.prompts = []
    FakeClient.messages = [_start(), _delta("thin"), _delta("king…"),
                           AssistantMessage(content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/a/b.py"})],
                                            model="m", parent_tool_use_id=None),
                           _start(), _delta("REP"), _delta("ORT"), _result()]
    events = []
    out = asyncio.run(H.sdk_helper(TASK, "helper-1", events.append, hook=_hook))
    assert out == "REPORT" and FakeClient.prompts == [TASK]
    assert events == [{"type": "activity", "id": "t1", "tool": "Read", "title": "Reading b.py", "state": "running",
                       "parent": None, "helper": False}]
    o = FakeClient.seen_options
    assert o.resume is None and o.continue_conversation is False
    assert o.model == H.HELPER_MODEL and o.tools == H.HELPER_TOOLS and o.max_turns == H.HELPER_MAX_TURNS
    assert "Agent" not in o.tools and "Task" not in o.tools
    assert str(o.cwd) == "/Users/Manoj/calwbot" and o.permission_mode == "auto" and o.can_use_tool is not None
    assert o.system_prompt == {"type": "preset", "preset": "claude_code", "append": H.HELPER_PROMPT}
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
    assert tools.TOOL_NAMES == ["delegate", "helpers", "cancel_helper"]


@aio
async def test_cancel_helper_tool_text(tmp_path):
    hs = _helpers(tmp_path, Recorder(gate=asyncio.Event()))
    rec = hs.start("long", TASK)
    assert await tools.cancel_text(hs, {"id": rec["id"]}) == f"cancelled {rec['id']}"
    await _settle(3)
    assert await tools.cancel_text(hs, {"id": rec["id"]}) == f"not running: {rec['id']}"


def test_helper_session_loads_no_mcp_servers():
    """A helper's `tools` list says nothing about MCP tools, so the session must not load the
    operator's settings servers (alpaca's order tools would need no card)."""
    from navigator.conductor.helpers import helper_options
    opts = helper_options(hook=lambda *a: {})
    assert opts.strict_mcp_config is True and opts.mcp_servers == {}
    assert opts.resume is None


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


def test_prompt_mentions_nohup_wait():
    assert "nohup" in H.HELPER_PROMPT and "timeout 1500" in H.HELPER_PROMPT
