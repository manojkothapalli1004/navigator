"""Crew screens (2026-11-04): each tool step of a turn becomes a short plain line for the website."""
import asyncio
import functools

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage

from navigator.conductor import activity, runner
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store


def test_titles_are_plain_and_never_show_a_command():
    d = activity.describe
    assert d("Bash", {"command": "curl -H 'x: token=abc' https://a", "description": "Check the bot log"}) == "Check the bot log"
    assert d("Bash", {"command": "curl https://api.telegram.org/botSECRET/getMe"}) == "Running a command"
    assert d("Read", {"file_path": "/Users/x/calwbot/manojtest/bot.py"}) == "Reading bot.py"
    assert d("Edit", {"file_path": "/a/b/Stage.tsx"}) == "Editing Stage.tsx"
    assert d("Write", {"file_path": "/a/b/notes.md"}) == "Writing notes.md"
    assert d("Grep", {"pattern": "secret"}) == "Searching the code"
    assert d("Glob", {"pattern": "**/*.py"}) == "Searching the code"
    assert d("WebSearch", {"query": "agent dashboards"}) == "Searching the web: agent dashboards"
    assert d("WebFetch", {"url": "https://example.com/a?b=c"}) == "Reading example.com"
    assert d("Agent", {"description": "Map ACT frontend", "prompt": "long…"}) == "Map ACT frontend"
    assert d("Task", {"description": "Audit the diff"}) == "Audit the diff"
    assert d("Skill", {"skill": "superpowers:brainstorming"}) == "Using skill superpowers:brainstorming"
    assert d("mcp__orchestrator__get_canonical_memory", {}) == "orchestrator: get canonical memory"
    assert d("TodoWrite", {"todos": []}) == "Updating the to-do list"
    assert d("SomethingNew", {"x": 1}) == "SomethingNew"


def test_titles_are_one_short_line():
    t = activity.describe("Bash", {"description": "line one\nline two " + "x" * 300})
    assert "\n" not in t and len(t) <= activity.MAX_TITLE and t.endswith("…")
    assert activity.describe("Bash", {"description": "   "}) == "Running a command"
    assert activity.describe("Read", {}) == "Reading a file"
    assert activity.describe("Read", None) == "Reading a file"


def test_a_bad_input_never_raises_and_token_shapes_are_hidden():
    # urlsplit raises on a bracketed host; that must not end the turn
    assert activity.describe("WebFetch", {"url": "http://[abc"}) == "WebFetch"
    t = activity.describe("Bash", {"description": "Retry with ghp_abcdefghijklmnopqrstuvwxyz0123 now"})
    assert "ghp_" not in t and t == "Retry with … now"
    assert "123456789:" not in activity.describe("WebSearch", {"query": "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"})


def test_helpers_are_the_agent_and_task_tools():
    assert activity.is_helper("Agent") and activity.is_helper("Task") and not activity.is_helper("Bash")


# ---- runner: tool calls and results become activity events ----

class FakeClient:
    messages: list = []
    def __init__(self, options):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def query(self, prompt):
        pass
    async def receive_response(self):
        for m in FakeClient.messages:
            yield m


def _result():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s", result="ok")


def test_runner_emits_running_then_done_or_failed(monkeypatch):
    monkeypatch.setattr(runner, "ClaudeSDKClient", FakeClient)
    FakeClient.messages = [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/x/y.py"}),
                                  ToolUseBlock(id="t2", name="Agent", input={"description": "Map the shell"})],
                         model="m", parent_tool_use_id=None),
        AssistantMessage(content=[ToolUseBlock(id="t3", name="Grep", input={"pattern": "a"})],
                         model="m", parent_tool_use_id="t2"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="...", is_error=False)]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t3", content="no", is_error=True)], parent_tool_use_id="t2"),
        UserMessage(content="plain text"),
        _result(),
    ]
    out = []
    asyncio.run(runner.sdk_turn("hi", None, out.append))
    acts = [e for e in out if e["type"] == "activity"]
    assert acts == [
        {"type": "activity", "id": "t1", "tool": "Read", "title": "Reading y.py", "state": "running", "parent": None, "helper": False},
        {"type": "activity", "id": "t2", "tool": "Agent", "title": "Map the shell", "state": "running", "parent": None, "helper": True},
        {"type": "activity", "id": "t3", "tool": "Grep", "title": "Searching the code", "state": "running", "parent": "t2", "helper": False},
        {"type": "activity", "id": "t1", "state": "done"},
        {"type": "activity", "id": "t3", "state": "failed"},
    ]


# ---- engine: activity is published live and kept for the turn, for windows that open mid-turn ----

def aio(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


class StepRunner:
    def __init__(self, gate=None):
        self.gate = gate
    async def __call__(self, prompt, session_id, emit):
        emit({"type": "activity", "id": "t1", "tool": "Read", "title": "Reading a.py", "state": "running",
              "parent": None, "helper": False})
        if self.gate is not None:
            await self.gate.wait()
        emit({"type": "activity", "id": "t1", "state": "done"})
        emit({"type": "delta", "text": "ok"})
        return "sess"


async def _drain(eng, n=200):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if not eng.busy and eng.queue_len == 0:
            return


@aio
async def test_engine_publishes_activity_and_keeps_the_turns_steps(tmp_path):
    gate = asyncio.Event()
    eng = Engine(Store(tmp_path / "c"), StepRunner(gate), lock_path=tmp_path / "c" / "turn.lock")
    await eng.start()
    q = eng.subscribe()
    m = eng.submit("look", "web")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if eng.activity_snapshot()["items"]:
            break
    snap = eng.activity_snapshot()
    assert snap["msg_id"] == m["id"]
    assert [(i["id"], i["title"], i["state"]) for i in snap["items"]] == [("t1", "Reading a.py", "running")]
    gate.set(); await _drain(eng)
    assert eng.activity_snapshot()["items"][0]["state"] == "done"
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    acts = [e for e in evs if e["type"] == "activity"]
    assert [a["item"]["state"] for a in acts] == ["running", "done"]
    assert all(a["msg_id"] == m["id"] and a["item"]["id"] == "t1" and a["item"]["ts"] for a in acts)
    assert acts[1]["item"]["title"] == "Reading a.py"  # the finished step still carries its title

    await eng.stop()


@aio
async def test_a_new_turn_starts_empty_even_before_its_first_step(tmp_path):
    class Once:
        n = 0
        async def __call__(self, prompt, session_id, emit):
            Once.n += 1
            if Once.n == 1:
                emit({"type": "activity", "id": "a1", "tool": "Read", "title": "Reading a", "state": "running",
                      "parent": None, "helper": False})
                emit({"type": "activity", "id": "a1", "state": "done"})
            return "s"
    eng = Engine(Store(tmp_path / "c"), Once(), lock_path=tmp_path / "c" / "turn.lock")
    await eng.start()
    eng.submit("one", "web"); await _drain(eng)
    m2 = eng.submit("two", "web"); await _drain(eng)  # no tool calls at all
    assert eng.activity_snapshot() == {"msg_id": m2["id"], "items": [], "helpers": []}
    await eng.stop()


@aio
async def test_steps_still_running_are_closed_when_the_turn_stops(tmp_path):
    gate = asyncio.Event()
    class Hang:
        async def __call__(self, prompt, session_id, emit):
            emit({"type": "activity", "id": "h1", "tool": "Agent", "title": "Helper", "state": "running",
                  "parent": None, "helper": True})
            if "stop" in prompt:
                await gate.wait()  # cancelled while waiting
            return "s"
    eng = Engine(Store(tmp_path / "c"), Hang(), lock_path=tmp_path / "c" / "turn.lock")
    await eng.start()
    eng.submit("stop me", "web")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if eng.activity_snapshot()["items"]:
            break
    assert eng.cancel()
    await _drain(eng)
    assert [i["state"] for i in eng.activity_snapshot()["items"]] == ["failed"]
    eng.submit("finish", "web"); await _drain(eng)  # a result never came back: closed as done with the turn
    assert [i["state"] for i in eng.activity_snapshot()["items"]] == ["done"]
    await eng.stop()


@aio
async def test_steps_leave_room_for_entries_in_a_slow_window(tmp_path):
    eng = Engine(Store(tmp_path / "c"), StepRunner(), lock_path=tmp_path / "c" / "turn.lock")
    q = eng.subscribe(maxsize=10)
    for i in range(30):
        eng._on_activity("m1", {"type": "activity", "id": f"t{i}", "tool": "Read", "title": "x",
                                "state": "running", "parent": None, "helper": False})
    assert not q.full()
    eng._append({"kind": "note", "text": "an entry still fits"})
    assert not eng.is_dropped(q)


@aio
async def test_engine_caps_the_kept_steps(tmp_path):
    class Many:
        async def __call__(self, prompt, session_id, emit):
            for i in range(activity.MAX_ITEMS + 50):
                emit({"type": "activity", "id": f"t{i}", "tool": "Read", "title": f"Reading {i}",
                      "state": "running", "parent": None, "helper": False})
            return "s"
    eng = Engine(Store(tmp_path / "c"), Many(), lock_path=tmp_path / "c" / "turn.lock")
    await eng.start()
    eng.submit("go", "web"); await _drain(eng)
    items = eng.activity_snapshot()["items"]
    assert len(items) == activity.MAX_ITEMS and items[-1]["id"] == f"t{activity.MAX_ITEMS + 49}"
    await eng.stop()


@aio
async def test_a_full_window_skips_steps_instead_of_being_dropped(tmp_path):
    # steps are like text deltas: a window that can't keep up misses some (the status read fills
    # them in), but it isn't cut off for it — only missed history entries do that
    eng = Engine(Store(tmp_path / "c"), StepRunner(), lock_path=tmp_path / "c" / "turn.lock")
    q = eng.subscribe(maxsize=3)
    for i in range(20):
        eng._on_activity("m1", {"type": "activity", "id": f"t{i}", "tool": "Read", "title": "Reading x",
                                "state": "running", "parent": None, "helper": False})
    assert not eng.is_dropped(q) and len(eng.activity_snapshot()["items"]) == 20


@aio
async def test_an_unknown_finished_step_is_ignored(tmp_path):
    class Stray:
        async def __call__(self, prompt, session_id, emit):
            emit({"type": "activity", "id": "nope", "state": "done"})  # its start was never seen
            return "s"
    eng = Engine(Store(tmp_path / "c"), Stray(), lock_path=tmp_path / "c" / "turn.lock")
    await eng.start()
    q = eng.subscribe()
    eng.submit("go", "web"); await _drain(eng)
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    assert not [e for e in evs if e["type"] == "activity"] and eng.activity_snapshot()["items"] == []
    await eng.stop()
