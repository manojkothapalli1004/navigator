import asyncio

from tests.conductor.test_engine import FakeRunner, _eng, _reng, aio


@aio
async def test_engine_note_appends_quietly(tmp_path):  # test 14
    runner = FakeRunner(); sent = []
    eng = _reng(tmp_path, runner, sent); await eng.start()
    eng.note("hello there")
    eng.note("tg too", via="telegram")
    await asyncio.sleep(0.05)
    notes = [e for e in eng.store.history(limit=100) if e.get("kind") == "note"]
    assert [n["text"] for n in notes] == ["hello there", "tg too"]
    assert not [e for e in eng.store.history(limit=100) if e.get("kind") == "user"]
    assert runner.calls == [] and eng.queue_len == 0
    assert sent == ["tg too"]
    await eng.stop()


# ---- §3c helpers: cap, ceiling, save ----
import logging  # noqa: E402

from tests.conductor.test_helpers import TASK, _helpers, _settle  # noqa: E402

EX = {"tool": "Bash", "input": {"command": "git merge x"}, "cwd": "/r"}


class Gated:
    """Runner that waits on a per-helper gate and records which helpers it was called for."""
    def __init__(self):
        self.gates, self.calls = {}, []

    async def __call__(self, task, helper_id, emit, exact=None):
        self.calls.append(helper_id)
        await self.gates.setdefault(helper_id, asyncio.Event()).wait()
        return "ok"

    def release(self, helper_id):
        self.gates.setdefault(helper_id, asyncio.Event()).set()


@aio
async def test_approved_helper_ignores_cap(tmp_path):  # test 6
    r = Gated(); hs = _helpers(tmp_path, r, max_running=2)
    a = hs.start("a", TASK); b = hs.start("b", TASK)
    ex = hs.start("approved card X", TASK, exact=EX)
    c = hs.start("c", TASK)
    assert (a["state"], b["state"], ex["state"], c["state"]) == ("running", "running", "running", "queued")
    await _settle(3)
    assert ex["id"] in r.calls and c["id"] not in r.calls
    for h in (a, b, ex, c):
        r.release(h["id"])
    await _settle(5)


@aio
async def test_exact_helpers_take_no_slot(tmp_path):  # test 7
    r = Gated(); hs = _helpers(tmp_path, r, max_running=1)
    ex = hs.start("ex", TASK, exact=EX)
    p1 = hs.start("p1", TASK)
    p2 = hs.start("p2", TASK)
    assert (ex["state"], p1["state"], p2["state"]) == ("running", "running", "queued")
    await _settle(3)
    r.release(ex["id"]); await _settle(5)
    assert p2["id"] not in r.calls  # the exact one's end starts nothing extra
    r.release(p1["id"]); await _settle(5)
    assert p2["id"] in r.calls
    r.release(p2["id"]); await _settle(5)


@aio
async def test_hard_ceiling_of_eight(tmp_path):  # test 15
    r = Gated(); hs = _helpers(tmp_path, r)
    exs = [hs.start(f"e{i}", TASK, exact=EX) for i in range(8)]
    assert all(e["state"] == "running" for e in exs)
    ninth = hs.start("e9", TASK, exact=EX)
    assert ninth["state"] == "queued"
    await _settle(3)
    assert ninth["id"] not in r.calls
    r.release(exs[0]["id"]); await _settle(5)
    assert ninth["id"] in r.calls
    for e in exs + [ninth]:
        r.release(e["id"])
    await _settle(5)


@aio
async def test_exact_queued_starts_before_plain_queued(tmp_path):  # test 15, per-record rule
    r = Gated(); hs = _helpers(tmp_path, r, max_running=3)
    plain = [hs.start(f"p{i}", TASK) for i in range(3)]
    exs = [hs.start(f"e{i}", TASK, exact=EX) for i in range(5)]
    assert all(e["state"] == "running" for e in exs)
    q_plain = hs.start("qp", TASK)            # 3 plain running: queued
    q_exact = hs.start("qe", TASK, exact=EX)  # 8 running: queued
    assert q_plain["state"] == q_exact["state"] == "queued"
    await _settle(3)
    r.release(exs[0]["id"]); await _settle(5)  # frees a ceiling slot but no plain slot
    assert q_exact["id"] in r.calls and q_plain["id"] not in r.calls
    for h in plain + exs + [q_plain, q_exact]:
        r.release(h["id"])
    await _settle(5)


@aio
async def test_save_failure_after_launch_does_not_raise(tmp_path, monkeypatch, caplog):  # test 16
    r = Gated(); hs = _helpers(tmp_path, r)

    def boom():
        raise OSError("disk-secret-xyz")
    monkeypatch.setattr(hs, "_save", boom)
    with caplog.at_level(logging.WARNING):
        out = hs.start("ex", TASK, exact=EX)
    assert out["state"] == "running"
    await _settle(3)
    assert r.calls == [out["id"]]
    assert "OSError" in caplog.text and "disk-secret-xyz" not in caplog.text
    r.release(out["id"]); await _settle(5)
