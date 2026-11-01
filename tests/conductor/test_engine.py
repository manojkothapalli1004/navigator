import asyncio
import fcntl
import functools
import re

import pytest

from navigator.conductor.engine import Engine, ResumeFailed
from navigator.conductor.store import Store


def aio(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


class FakeRunner:
    def __init__(self, gate=None, fail=None):
        self.calls, self.gate, self.fail = [], gate, fail or {}
    async def __call__(self, prompt, session_id, emit):
        self.raw = getattr(self, "raw", []) + [prompt]
        prompt = re.sub(r"^\[(web|telegram|terminal)\] ", "", prompt)  # the engine's source prefix
        self.calls.append((prompt, session_id))
        if self.gate is not None:
            await self.gate.wait()
        f = self.fail.pop(prompt, None) if isinstance(self.fail, dict) else None
        if f == "resume" and session_id:
            raise ResumeFailed("gone")
        if f == "boom":
            raise RuntimeError("sdk exploded")
        emit({"type": "delta", "text": f"re:{prompt[:2]}"})
        return "sess-" + str(len(self.calls))


def _eng(tmp_path, runner):
    return Engine(Store(tmp_path / "c"), runner, lock_path=tmp_path / "c" / "turn.lock")


async def _drain(eng, n=200):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if not eng.busy and eng.queue_len == 0:
            return


def _status(store, msg_id):
    return [e["state"] for e in store.history(limit=1000) if e.get("kind") == "status" and e.get("msg_id") == msg_id]


@aio
async def test_one_turn_at_a_time_fifo(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _eng(tmp_path, r); await eng.start()
    a = eng.submit("aa", "web"); b = eng.submit("bb", "telegram")
    await asyncio.sleep(0.05)
    assert [c[0] for c in r.calls] == ["aa"]
    gate.set(); await _drain(eng)
    assert [c[0] for c in r.calls] == ["aa", "bb"]
    texts = [e["text"] for e in eng.store.history() if e["kind"] == "assistant"]
    assert texts == ["re:aa", "re:bb"]
    assert _status(eng.store, a["id"]) == ["queued", "running", "done"]
    await eng.stop()


@aio
async def test_queued_position_reported(tmp_path):
    gate = asyncio.Event(); eng = _eng(tmp_path, FakeRunner(gate)); await eng.start()
    eng.submit("aa", "web"); await asyncio.sleep(0.02)
    b = eng.submit("bb", "web"); c = eng.submit("cc", "web")
    q = {e["msg_id"]: e.get("detail") for e in eng.store.history() if e.get("state") == "queued"}
    assert q[b["id"]] == 1 and q[c["id"]] == 2
    gate.set(); await _drain(eng); await eng.stop()


@aio
async def test_session_id_persisted_and_reused(tmp_path):
    r = FakeRunner(); eng = _eng(tmp_path, r); await eng.start()
    eng.submit("aa", "web"); await _drain(eng)
    eng.submit("bb", "web"); await _drain(eng)
    assert r.calls == [("aa", None), ("bb", "sess-1")]
    assert eng.store.session_id == "sess-2"
    await eng.stop()


@aio
async def test_resume_failure_starts_fresh(tmp_path):
    r = FakeRunner(fail={"bb": "resume"}); eng = _eng(tmp_path, r)
    eng.store.set_session_id("old"); await eng.start()
    m = eng.submit("bb", "web"); await _drain(eng)
    assert r.calls == [("bb", "old"), ("bb", None)]
    assert any(e["kind"] == "note" and "could not be resumed" in e["text"] for e in eng.store.history())
    assert _status(eng.store, m["id"])[-1] == "done"
    await eng.stop()


@aio
async def test_runner_error_keeps_worker_alive(tmp_path):
    r = FakeRunner(fail={"aa": "boom"}); eng = _eng(tmp_path, r); await eng.start()
    a = eng.submit("aa", "web"); b = eng.submit("bb", "web"); await _drain(eng)
    err = [e for e in eng.store.history() if e.get("msg_id") == a["id"] and e.get("state") == "error"]
    assert err and "RuntimeError" in err[0]["detail"]
    assert _status(eng.store, b["id"])[-1] == "done"
    await eng.stop()


@aio
async def test_restart_marks_interrupted(tmp_path):
    s = Store(tmp_path / "c")
    u = s.append({"kind": "user", "text": "x", "source": "web"})
    s.append({"kind": "status", "state": "queued", "msg_id": u["id"]})
    s.append({"kind": "status", "state": "running", "msg_id": u["id"]})
    r = FakeRunner(); eng = _eng(tmp_path, r); await eng.start()
    await _drain(eng)
    assert _status(eng.store, u["id"])[-1] == "interrupted" and r.calls == []
    await eng.stop()


@aio
async def test_slow_subscriber_does_not_block(tmp_path):
    r = FakeRunner(); eng = _eng(tmp_path, r); await eng.start()
    slow = eng.subscribe(maxsize=3)
    for i in range(5):
        eng.submit(f"m{i}", "web")
    await _drain(eng)
    assert len(r.calls) == 5
    await eng.stop()


@aio
async def test_turn_holds_lock(tmp_path):
    gate = asyncio.Event(); eng = _eng(tmp_path, FakeRunner(gate)); await eng.start()
    eng.submit("aa", "web"); await asyncio.sleep(0.05)
    with open(tmp_path / "c" / "turn.lock", "w") as fh:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gate.set(); await _drain(eng)
    with open(tmp_path / "c" / "turn.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    await eng.stop()


@aio
async def test_events_published(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    q = eng.subscribe()
    m = eng.submit("aa", "web"); await _drain(eng)
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    assert {"type": "delta", "msg_id": m["id"], "text": "re:aa"} in evs
    assert any(e["type"] == "entry" and e["entry"]["kind"] == "assistant" for e in evs)
    await eng.stop()


@aio
async def test_hung_turn_times_out(tmp_path):
    gate = asyncio.Event()
    eng = Engine(Store(tmp_path / "c"), FakeRunner(gate), lock_path=tmp_path / "c" / "turn.lock", turn_timeout_s=0.1)
    await eng.start()
    a = eng.submit("aa", "web"); await _drain(eng)
    err = [e for e in eng.store.history() if e.get("msg_id") == a["id"] and e.get("state") == "error"]
    assert err and "timed out" in err[0]["detail"]
    gate.set(); b = eng.submit("bb", "web"); await _drain(eng)
    assert _status(eng.store, b["id"])[-1] == "done"
    await eng.stop()


@aio
async def test_dropped_subscriber_is_flagged(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    q = eng.subscribe(maxsize=1)
    eng.submit("aa", "web"); await _drain(eng)
    assert eng.is_dropped(q)
    await eng.stop()


@aio
async def test_prompt_prefixed_with_source(tmp_path):
    r = FakeRunner(); eng = _eng(tmp_path, r); await eng.start()
    eng.submit("hello", "telegram"); await _drain(eng)
    assert r.raw == ["[telegram] hello"]
    assert [e["text"] for e in eng.store.history() if e["kind"] == "user"] == ["hello"]


@aio
async def test_cancel_running_turn_marks_error_and_next_runs(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _eng(tmp_path, r); await eng.start()
    a = eng.submit("aa", "web"); await asyncio.sleep(0.05)
    assert eng.cancel() is True
    gate.set(); b = eng.submit("bb", "web"); await _drain(eng)
    assert _status(eng.store, a["id"])[-1] == "error"
    assert any(e.get("detail") == "cancelled by operator" for e in eng.store.history() if e.get("msg_id") == a["id"])
    assert _status(eng.store, b["id"])[-1] == "done"


@aio
async def test_cancel_idle_returns_false(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    assert eng.cancel() is False


@aio
async def test_cancel_keeps_previous_session_id(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    eng.submit("aa", "web"); await _drain(eng)
    sid = eng.store.session_id
    gate = asyncio.Event(); eng.runner = FakeRunner(gate)
    eng.submit("bb", "web"); await asyncio.sleep(0.05)
    eng.cancel(); await _drain(eng)
    assert eng.store.session_id == sid


@aio
async def test_internal_approval_source_only(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    with pytest.raises(ValueError):
        eng.submit("run it", "approval")
    u = eng.submit("run it", "approval", internal=True); await _drain(eng)
    assert u["source"] == "approval" and eng.runner.raw == ["[approval] run it"]


@aio
async def test_card_entry_published_without_fingerprint(tmp_path):
    sent = []
    eng = Engine(Store(tmp_path / "c"), FakeRunner(), lock_path=tmp_path / "c" / "turn.lock", notify=sent.append)
    await eng.start()
    eng.on_card({"id": "i", "code": "ABC234", "state": "pending", "fingerprint": "f", "summary": "git push"})
    eng.on_card({"id": "i", "code": "ABC234", "state": "used", "fingerprint": "f", "summary": "git push"})
    cards = [e for e in eng.store.history() if e["kind"] == "card"]
    assert [c["state"] for c in cards] == ["pending", "used"] and "fingerprint" not in cards[0]
    # each state change is its own history entry; the card is named by card_id
    assert cards[0]["id"] != cards[1]["id"] and {c["card_id"] for c in cards} == {"i"}
    assert [c["state"] for c in sent] == ["pending"]  # only a new card goes to Telegram
