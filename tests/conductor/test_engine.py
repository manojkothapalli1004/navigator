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


# --- C4: replies go back to Telegram for Telegram-originated turns only ---

def _reng(tmp_path, runner, sent):
    return Engine(Store(tmp_path / "c"), runner, lock_path=tmp_path / "c" / "turn.lock", reply=sent.append)


@aio
async def test_telegram_turn_reply_sent(tmp_path):
    sent = []; eng = _reng(tmp_path, FakeRunner(), sent); await eng.start()
    eng.submit("hello", "telegram"); await _drain(eng); await eng.stop()
    assert sent == ["re:he"]


@aio
async def test_web_turn_not_sent_to_telegram(tmp_path):
    sent = []; eng = _reng(tmp_path, FakeRunner(), sent); await eng.start()
    eng.submit("hello", "web"); eng.submit("hi", "terminal"); await _drain(eng); await eng.stop()
    assert sent == []


@aio
async def test_telegram_turn_error_sends_stopped(tmp_path):
    sent = []; eng = _reng(tmp_path, FakeRunner(fail={"x": "boom"}), sent); await eng.start()
    eng.submit("x", "telegram"); await _drain(eng); await eng.stop()
    assert sent == ["Stopped: RuntimeError: sdk exploded"]


@aio
async def test_approval_via_telegram_replies(tmp_path):
    sent = []; eng = _reng(tmp_path, FakeRunner(), sent); await eng.start()
    u = eng.submit("Card ABC234 was approved", "approval", internal=True, reply_via="telegram")
    eng.submit("Card XYZ789 was approved", "approval", internal=True, reply_via="web")
    await _drain(eng); await eng.stop()
    assert u["reply_via"] == "telegram" and sent == ["re:[a"]  # FakeRunner keeps the [approval] prefix


@aio
async def test_interrupted_telegram_turn_notifies(tmp_path):
    s = Store(tmp_path / "c")
    t = s.append({"kind": "user", "text": "x", "source": "telegram"})
    s.append({"kind": "status", "state": "running", "msg_id": t["id"]})
    w = s.append({"kind": "user", "text": "y", "source": "web"})
    s.append({"kind": "status", "state": "queued", "msg_id": w["id"]})
    sent = []; eng = _reng(tmp_path, FakeRunner(), sent); await eng.start(); await eng.stop()
    assert sent == ["Interrupted: the conductor restarted before this finished; send it again if needed"]


@aio
async def test_reply_failure_does_not_break_turn(tmp_path):
    def bad(text):
        raise OSError("net down")
    eng = Engine(Store(tmp_path / "c"), FakeRunner(), lock_path=tmp_path / "c" / "turn.lock", reply=bad)
    await eng.start(); m = eng.submit("hello", "telegram"); await _drain(eng); await eng.stop()
    assert _status(eng.store, m["id"])[-1] == "done"


# --- C5: the terminal joins the shared conversation ---

@aio
async def test_terminal_join_left_imports_entries(tmp_path):
    r = FakeRunner(); eng = _eng(tmp_path, r); await eng.start()
    eng.terminal_joined()
    assert eng.terminal is True
    n = eng.terminal_left([{"kind": "user", "text": "hi"}, {"kind": "assistant", "text": "hello"}])
    await _drain(eng); await eng.stop()
    h = eng.store.history()
    assert n == 2 and eng.terminal is False
    assert h[0]["kind"] == "note" and h[0]["text"] == "Operator is in the terminal — replies wait until they leave."
    assert (h[1]["kind"], h[1]["text"], h[1]["source"], h[1]["imported"]) == ("user", "hi", "terminal", True)
    assert (h[2]["kind"], h[2]["text"], h[2]["imported"]) == ("assistant", "hello", True)
    assert h[3]["text"] == "Operator left the terminal."
    assert r.calls == []  # imported entries are never run as turns


@aio
async def test_message_while_terminal_queued_detail(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    eng.terminal_joined()
    with open(tmp_path / "c" / "turn.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # the terminal holds the lock
        m = eng.submit("aa", "web"); await asyncio.sleep(0.05)
        q = [e for e in eng.store.history() if e.get("msg_id") == m["id"] and e.get("state") == "queued"]
        assert q[0]["detail"] == "waiting for the terminal session to end"
        assert _status(eng.store, m["id"]) == ["queued"]
        eng.terminal_left([])
    await _drain(eng); await eng.stop()
    assert _status(eng.store, m["id"])[-1] == "done"


@aio
async def test_telegram_message_while_terminal_notified(tmp_path):
    sent = []; eng = _reng(tmp_path, FakeRunner(), sent); await eng.start()
    eng.terminal_joined()
    with open(tmp_path / "c" / "turn.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        eng.submit("aa", "telegram"); await asyncio.sleep(0.05)
        assert sent == ["You're in the terminal — I'll answer this when you leave it."]
        eng.terminal_left([])
    await _drain(eng); await eng.stop()


@aio
async def test_turn_after_dead_terminal_clears_flag(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    eng.terminal_joined()  # ...and the terminal died: no terminal_left, lock already free
    m = eng.submit("aa", "web"); await _drain(eng); await eng.stop()
    notes = [e["text"] for e in eng.store.history() if e["kind"] == "note"]
    assert "Terminal session ended without saying goodbye; its turns aren't in this history." in notes
    assert eng.terminal is False and _status(eng.store, m["id"])[-1] == "done"


# --- 2026-11-02 polish: one "Stopped" on Telegram after /stop ---

@aio
async def test_telegram_cancel_sends_no_second_reply(tmp_path):
    sent = []; gate = asyncio.Event()
    eng = _reng(tmp_path, FakeRunner(gate), sent); await eng.start()
    m = eng.submit("aa", "telegram"); await asyncio.sleep(0.05)
    assert eng.cancel(via="telegram") is True  # the bot already answered "Stopped."
    gate.set(); await _drain(eng); await eng.stop()
    assert sent == [] and _status(eng.store, m["id"])[-1] == "error"
    assert any(e.get("detail") == "cancelled by operator" for e in eng.store.history() if e.get("msg_id") == m["id"])


@aio
async def test_web_cancel_of_telegram_turn_still_replies(tmp_path):
    sent = []; gate = asyncio.Event()
    eng = _reng(tmp_path, FakeRunner(gate), sent); await eng.start()
    eng.submit("aa", "telegram"); await asyncio.sleep(0.05)
    assert eng.cancel() is True  # no via (the web's Stop button): unchanged
    gate.set(); await _drain(eng); await eng.stop()
    assert sent == ["Stopped: cancelled by operator"]


@aio
async def test_telegram_cancel_flag_does_not_leak_to_next_turn(tmp_path):
    sent = []; gate = asyncio.Event()
    eng = _reng(tmp_path, FakeRunner(gate, fail={"bb": "boom"}), sent); await eng.start()
    eng.submit("aa", "telegram"); await asyncio.sleep(0.05)
    eng.cancel(via="telegram"); gate.set()
    eng.submit("bb", "telegram"); await _drain(eng); await eng.stop()
    assert sent == ["Stopped: RuntimeError: sdk exploded"]  # a real error after a /stop still replies
