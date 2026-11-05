"""Stage 2 part 2: delegate kind=build, helper files, engine build wiring, autopilot overlap + cap."""
import asyncio
import functools
import pathlib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.conductor import builds as B
from navigator.conductor import tools
from navigator.conductor.autopilot import Autopilot, WorkItem, blocked_by
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store
from navigator.contracts.enums import RunPhase, RunStatus
from navigator.contracts.run import Run

SPEC = "x" * 500
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo("America/New_York"))


def aio(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


class Cfg:
    tier_c_paths = ()
    tier_floor = "A"


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: Cfg() if s is not None else None)


class FakeHelpers:
    def __init__(self):
        self.started = []

    def start(self, title, task, **kw):
        self.started.append((title, task, kw))
        return {"id": "helper-1", "state": "running", "title": title}

    def snapshot(self):
        return []

    def running_work(self):
        return []


def mkbuilds(tmp_path, act=None, **kw):
    act = act or FakeACTClient()
    return B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda r: "s", **kw), act


# ---- tools (1, 2, 4)
@aio
async def test_delegate_build_files_one_proposal_and_no_helper(tmp_path):
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)
    r = await tools.delegate_text(hs, {"kind": "build", "project": "navigator", "title": "T",
                                       "task": SPEC, "files": ["navigator/a.py"]}, bs)
    (p,) = act.proposals.values()
    assert p.origin == "operator" and p.repo == B.BUILD_REPOS["navigator"] and p.intended_changes == SPEC
    assert p.affected_files == ["navigator/a.py"] and p.id in r and "waiting for operator approval" in r
    assert hs.started == []


@aio
async def test_delegate_build_tier1_reply(tmp_path):
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)
    r = await tools.delegate_text(hs, {"kind": "build", "project": "navigator", "title": "T",
                                       "task": SPEC, "files": ["docs/x.md"]}, bs)
    assert "tier 1" in r and "will not merge without your approval" in r


@aio
async def test_delegate_build_refusals_never_fall_back(tmp_path):
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)
    base = {"kind": "build", "project": "navigator", "title": "t", "task": SPEC}
    for over in ({"project": "trader"}, {"project": "nope"}, {"task": "short"}, {"title": ""}):
        assert (await tools.delegate_text(hs, {**base, **over}, bs)).startswith("refused")
    assert "builds are not configured" in await tools.delegate_text(hs, base, None)
    assert (await tools.delegate_text(hs, {**base, "kind": "weird"}, bs)) == "refused: kind must be quick or build"
    assert act.proposals == {} and hs.started == []


@aio
async def test_delegate_build_act_down(tmp_path):
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)

    def boom(p):
        raise ConnectionError("down")
    act.create_proposal = boom
    r = await tools.delegate_text(hs, {"kind": "build", "project": "navigator", "title": "t", "task": SPEC}, bs)
    assert r == "refused: ACT is not reachable (NAVIGATOR_ACT_BASE_URL)" and hs.started == []


@aio
async def test_delegate_quick_unchanged_and_passes_files(tmp_path):
    hs = FakeHelpers()
    task = "Read navigator/conductor/engine.py and report what the worker does."
    r = await tools.delegate_text(hs, {"title": "t", "task": task})
    assert r == "started helper-1 (running): t" and hs.started[0][2].get("files") is None
    await tools.delegate_text(hs, {"kind": "quick", "title": "t", "task": task, "files": ["a/b.py"]})
    assert hs.started[1][2]["files"] == ["a/b.py"]


@aio
async def test_helpers_text_lists_builds(tmp_path):
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)
    await tools.delegate_text(hs, {"kind": "build", "project": "navigator", "title": "Tt", "task": SPEC}, bs)
    out = await tools.helpers_text(hs, bs)
    assert "build " in out and "undeclared" in out and "Tt" in out


def test_server_takes_builds():
    cfg = tools.make_conductor_server(FakeHelpers(), None)
    assert cfg["name"] == "conductor"


# ---- helpers carry files (b)
def test_helpers_record_files_and_running_work(tmp_path):
    from navigator.conductor import helpers as H

    async def run():
        async def runner(task, hid, emit, exact=None):
            await asyncio.sleep(10)
        hs = H.Helpers(tmp_path / "h.json", runner, lambda *a: None, lambda r: None)
        hs.start("t", "task", files=["a/b.py"])
        hs.start("u", "task")
        assert {w.files for w in hs.running_work()} == {frozenset({"a/b.py"}), None}
        assert hs.snapshot()
        hs.shutdown()
    asyncio.run(run())


# ---- autopilot (8, 12-15)
def _ap(tmp_path, plan="- [ ] one\n  Files: a.py, b.py\n- [ ] two\n"):
    (tmp_path / "plan.md").write_text(plan)
    return Autopilot(tmp_path / "s.json", tmp_path / "plan.md", clock=lambda: NOON)


def W(i, files):
    return WorkItem("build", i, None if files is None else frozenset(files))


def test_decide_compat_work_running(tmp_path):
    ap = _ap(tmp_path)
    ap.set(True)
    before = dict(ap.state)
    assert ap.decide("done", False, work_running=True) == ("wait", "a build or helper is still running")
    assert ap.state == before
    assert ap.runnable(False, work_running=True) == "a build or helper is still running"
    assert ap.decide("done", False)[0] == "queue"
    assert ap.decide("done", False, False)[0] == "queue"


def test_overlap_waits_and_dir_containment():
    assert blocked_by(frozenset({"a.py", "b.py"}), [W("build-1", {"b.py"})]) == "overlaps build-1 on b.py"
    assert blocked_by(frozenset({"a/"}), [W("x", {"a/x.py"})]).startswith("overlaps x")
    assert blocked_by(frozenset({"a/x.py"}), [W("x", {"a"})]).startswith("overlaps x")
    assert blocked_by(frozenset({"ab/x.py"}), [W("x", {"a"})]) is None


def test_disjoint_runs(tmp_path):
    assert blocked_by(frozenset({"a.py"}), [W("x", {"b.py"})]) is None
    ap = _ap(tmp_path)
    ap.set(True)
    assert ap.decide("done", False, item_files=frozenset({"a.py"}), running=[W("x", {"b.py"})])[0] == "queue"


def test_undeclared_waits():
    assert "does not declare its files" in blocked_by(None, [W("x", {"b.py"})])
    assert "does not declare" in blocked_by(frozenset({"a.py"}), [W("x", None)])
    assert blocked_by(None, []) is None


def test_cap_counts_running_work_only():
    three = [W(f"x{i}", {f"f{i}.py"}) for i in range(3)]
    assert blocked_by(frozenset({"z.py"}), three) == "3 helpers/builds already running"
    assert blocked_by(frozenset({"z.py"}), three[:2]) is None


def test_filed_and_review_builds_do_not_block(tmp_path):
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] item\n")  # undeclared item
    rec = bs.file("navigator", "T", SPEC, ["b.py"], None)
    assert rec["state"] == "filed" and eng.running_work() == []
    assert blocked_by(None, eng.running_work()) is None
    bs._records[0]["state"] = "review"
    assert eng.running_work() == []
    bs._records[0]["state"] = "running"
    assert [w.id for w in eng.running_work()] == [rec["proposal_id"]]


def test_running_helper_still_blocks_undeclared_item():
    assert "does not declare its files" in blocked_by(None, [WorkItem("helper", "h", frozenset({"a.py"}))])
    assert blocked_by(None, []) is None


@aio
async def test_delegate_build_filing_runs_off_loop_and_errors_are_narrow(tmp_path):
    import httpx
    hs = FakeHelpers()
    bs, act = mkbuilds(tmp_path)
    seen = {}
    orig = bs.file

    def spy(*a, **k):
        seen["t"] = threading.get_ident()
        return orig(*a, **k)
    bs.file = spy
    args = {"kind": "build", "project": "navigator", "title": "t", "task": SPEC}
    await tools.delegate_text(hs, args, bs)
    assert seen["t"] != threading.get_ident()

    def odd(p):
        raise KeyError("x")
    act.create_proposal = odd
    assert await tools.delegate_text(hs, args, bs) == "refused: build filing failed (KeyError)"

    def down(p):
        raise httpx.ConnectError("down")
    act.create_proposal = down
    assert await tools.delegate_text(hs, args, bs) == tools.ACT_DOWN


def test_make_builds_survives_bad_repos_file(tmp_path, monkeypatch, capsys):
    from navigator.conductor import server
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("NAVIGATOR_ACT_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(bad))
    assert server.make_builds(tmp_path) is None
    assert "builds" in capsys.readouterr().err


def test_file_persists_record_before_approving(tmp_path):
    bs, act = mkbuilds(tmp_path)
    seen = {}
    orig = act.set_proposal_status

    def spy(pid, status):
        seen["saved"] = [r["proposal_id"] for r in B.Builds(tmp_path / "builds.json", act)._records]
        return orig(pid, status)
    act.set_proposal_status = spy
    rec = bs.file("navigator", "T", SPEC, ["docs/x.md"], None)
    assert rec["tier1"] and seen["saved"] == [rec["proposal_id"]]


@aio
async def test_on_build_finish_masks_title(tmp_path):
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] x\n")
    await eng.start()
    eng.on_build_finish({"proposal_id": "p1", "state": "done", "summary": "ok",
                         "title": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"})
    await _drain()
    users = [e for e in eng.store.history(limit=50) if e.get("kind") == "user"]
    assert "sk-ant" not in users[0]["text"]
    await eng.stop()


def test_helpers_has_one_running_work():
    import inspect
    from navigator.conductor import helpers as H
    assert inspect.getsource(H).count("def running_work") == 1


def test_decide_reads_plan_files(tmp_path):
    ap = _ap(tmp_path)
    ap.set(True)
    assert ap.decide("done", False, running=[W("x", {"b.py"})]) == ("wait", "overlaps x on b.py")
    assert ap.state["consecutive"] == 0
    assert ap.decide("done", False, running=[W("x", {"c.py"})])[0] == "queue"
    ap2 = _ap(tmp_path, plan="- [ ] one\n")
    ap2.set(True)
    assert ap2.decide("done", False, running=[W("x", {"c.py"})])[0] == "wait"


# ---- engine (9, 10, 11, 21)
class Runner:
    def __init__(self):
        self.raw = []

    async def __call__(self, prompt, sid, emit):
        self.raw.append(prompt)
        emit({"type": "delta", "text": "ok"})
        return "s"


async def _drain(n=60):
    for _ in range(n):
        await asyncio.sleep(0.01)


def mkeng(tmp_path, plan, sent=None):
    (tmp_path / "plan.md").write_text(plan)
    ap = Autopilot(tmp_path / "ap.json", tmp_path / "plan.md", clock=lambda: NOON)
    act = FakeACTClient()
    bs = B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda r: "s")
    eng = Engine(Store(tmp_path / "c"), Runner(), lock_path=tmp_path / "c" / "turn.lock", autopilot=ap,
                 reply=(sent.append if sent is not None else None), builds=bs)
    eng.build_poll_s = 3600
    return eng, ap, bs, act


@aio
async def test_engine_overlap_blocks_then_resume_on_finish(tmp_path):
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] item\n  Files: b.py\n")
    ap.set(True)
    rec = bs.file("navigator", "T", SPEC, ["b.py"], None)
    live = Run(proposal_id=rec["proposal_id"], base_commit_sha="a", branch="b", status=RunStatus.executing,
               phase=RunPhase.building)
    act.runs[live.id] = live
    await asyncio.to_thread(bs.poll)
    assert [w.id for w in eng.running_work()] == [rec["proposal_id"]]
    await eng.start()
    eng.submit("hi", "web")
    await _drain()
    assert not any(p.startswith("[autopilot]") for p in eng.runner.raw)
    r = live
    r.status, r.phase = RunStatus.completed, RunPhase.finalizing
    await asyncio.to_thread(bs.poll)
    await _drain()
    raws = eng.runner.raw
    assert any(p.startswith("[helper] [build ") and " done: T" in p for p in raws)
    assert any(p.startswith("[autopilot]") for p in raws)
    await eng.stop()


@aio
async def test_engine_nonoverlap_queues(tmp_path):
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] item\n  Files: z.py\n")
    ap.set(True)
    bs.file("navigator", "T", SPEC, ["b.py"], None)
    await eng.start()
    eng.submit("hi", "web")
    await _drain()
    assert any(p.startswith("[autopilot]") for p in eng.runner.raw)
    await eng.stop()


@aio
async def test_on_finish_reaches_telegram_window_failed_and_masked(tmp_path):
    sent = []
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] x\n", sent)
    await eng.start()
    eng.on_build_finish({"proposal_id": "p1", "state": "failed", "title": "T", "reply_via": "telegram",
                         "summary": "boom sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 end"})
    await _drain()
    users = [e for e in eng.store.history(limit=50) if e.get("kind") == "user"]
    assert users[0]["source"] == "helper" and users[0]["reply_via"] == "telegram"
    assert users[0]["text"].startswith("[build p1 failed: T]") and "sk-ant" not in users[0]["text"]
    assert "ok" in sent
    await eng.stop()


@aio
async def test_poll_off_loop_and_finish_on_loop_and_review_does_not_block(tmp_path):
    eng, ap, bs, act = mkeng(tmp_path, "- [ ] x\n")
    seen = {}
    orig = eng.on_build_finish
    eng.on_build_finish = lambda rec: (seen.update(t=threading.get_ident()), orig(rec))
    rec = bs.file("navigator", "T", SPEC, [], None)
    r = Run(proposal_id=rec["proposal_id"], base_commit_sha="a", branch="b", status=RunStatus.executing,
            phase=RunPhase.awaiting_review)
    act.runs[r.id] = r
    eng.build_poll_s = 0.01
    await eng.start()
    await _drain(30)
    assert bs.loop is asyncio.get_running_loop() and seen["t"] == threading.get_ident()
    assert bs.snapshot()[0]["state"] == "review" and eng.running_work() == []
    await eng.stop()


@aio
async def test_no_builds_registry_when_not_configured(tmp_path):
    eng = Engine(Store(tmp_path / "c"), Runner(), lock_path=tmp_path / "c" / "turn.lock")
    assert eng.running_work() == []
    await eng.start()
    await eng.stop()


def test_no_merge_or_approval_calls_in_conductor_package():
    root = pathlib.Path(B.__file__).parent
    for f in root.glob("*.py"):
        src = f.read_text()
        assert "decide_approval" not in src, f
        if f.name != "builds.py":
            assert "set_proposal_status" not in src, f
