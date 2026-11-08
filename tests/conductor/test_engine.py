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
        prompt = re.sub(r"^\[(web|telegram|terminal|helper)\] ", "", prompt)  # the engine's source prefix
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


# --- 2026-11-02 autopilot wiring ---

from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _Z  # noqa: E402
from navigator.conductor.autopilot import Autopilot  # noqa: E402


def _apeng(tmp_path, runner, sent=None, cards_pending=lambda: False, plan="- [ ] item one\n- [ ] item two\n"):
    (tmp_path / "plan.md").write_text(plan)
    ap = Autopilot(tmp_path / "ap.json", tmp_path / "plan.md",
                   clock=lambda: _dt(2026, 11, 2, 12, 0, tzinfo=_Z("America/New_York")))
    sent = [] if sent is None else sent
    eng = Engine(Store(tmp_path / "c"), runner, lock_path=tmp_path / "c" / "turn.lock", reply=sent.append,
                 autopilot=ap, cards_pending=cards_pending)
    return eng, ap, sent


@aio
async def test_autopilot_off_queues_nothing(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); await eng.start()
    eng.submit("hello", "web"); await _drain(eng); await eng.stop()
    assert len(r.calls) == 1


@aio
async def test_autopilot_continues_and_is_labeled(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); ap.set(True); await eng.start()
    eng.submit("hello", "web"); await _drain(eng)
    ap.set(False); await _drain(eng); await eng.stop()
    users = [e for e in eng.store.history() if e["kind"] == "user"]
    assert users[1]["source"] == "autopilot" and "item one" in users[1]["text"]
    assert r.raw[1].startswith("[autopilot] ")
    assert any(s.startswith("Autopilot: ") for s in sent)  # labeled on Telegram


@aio
async def test_autopilot_not_after_error(tmp_path):
    r = FakeRunner(fail={"boom it": "boom"}); eng, ap, sent = _apeng(tmp_path, r); ap.set(True); await eng.start()
    eng.submit("boom it", "web"); await _drain(eng); await eng.stop()
    assert [e["source"] for e in eng.store.history() if e["kind"] == "user"] == ["web"]


@aio
async def test_stop_turns_autopilot_off(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng, ap, sent = _apeng(tmp_path, r); ap.set(True)
    await eng.start(); eng.submit("aa", "web"); await asyncio.sleep(0.05)
    eng.cancel(); gate.set(); await _drain(eng); await eng.stop()
    assert ap.on is False
    assert [e["source"] for e in eng.store.history() if e["kind"] == "user"] == ["web"]


@aio
async def test_autopilot_waits_for_pending_card(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r, cards_pending=lambda: True); ap.set(True)
    await eng.start(); eng.submit("hello", "web"); await _drain(eng); await eng.stop()
    assert len(r.calls) == 1 and ap.on is True


@aio
async def test_cap_turns_off_with_one_telegram_line(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); ap.set(True); await eng.start()
    eng.submit("hello", "web")
    for _ in range(40):
        await _drain(eng)
    await eng.stop()
    sources = [e["source"] for e in eng.store.history() if e["kind"] == "user"]
    assert sources.count("autopilot") == 6 and ap.on is False
    offs = [s for s in sent if "turned off" in s]
    assert len(offs) == 1 and "6 autopilot turns in a row" in offs[0]


@aio
async def test_autopilot_words_never_become_turns(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); await eng.start()
    eng.submit("autopilot on", "telegram"); await _drain(eng)
    assert ap.on is True and r.calls == []
    eng.submit("autopilot status", "web"); eng.submit("autopilot off", "web"); await _drain(eng); await eng.stop()
    assert ap.on is False and r.calls == []
    notes = [e["text"] for e in eng.store.history() if e["kind"] == "note"]
    assert any(n.startswith("Autopilot on") for n in notes) and any(n.startswith("Autopilot off") for n in notes)
    assert any("Autopilot on" in s for s in sent)  # the Telegram sender hears its own toggle


@aio
async def test_internal_sources_cannot_toggle(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); await eng.start()
    eng.submit("autopilot on", "approval", internal=True); await _drain(eng); await eng.stop()
    assert ap.on is False  # only the operator's windows switch it


# --- autopilot cold-audit fixes ---

@aio
async def test_autopilot_never_piles_up_behind_operator_messages(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng, ap, sent = _apeng(tmp_path, r); ap.set(True)
    await eng.start()
    eng.submit("m1", "web"); await asyncio.sleep(0.05)
    eng.submit("m2", "web")          # queued while m1 runs
    gate.set(); await _drain(eng)
    ap.set(False); await _drain(eng); await eng.stop()
    hist = eng.store.history(limit=1000)
    users = [e for e in hist if e["kind"] == "user"]
    assert [u["source"] for u in users][:3] == ["web", "web", "autopilot"]  # m1 done -> m2 first
    ap_ids = {u["id"] for u in users if u["source"] == "autopilot"}
    ahead = [e["detail"] for e in hist if e.get("state") == "queued" and e.get("msg_id") in ap_ids]
    # queued from the finishing turn itself (counts as 1, busy) and never behind another waiting turn (would be 2+)
    assert ahead and all(a == 1 for a in ahead)


@aio
async def test_autopilot_turn_rechecks_when_it_starts(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); ap.set(True)
    lock = open(tmp_path / "c" / "turn.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # e.g. the operator is in the terminal: the turn waits on the lock
    await eng.start()
    eng._after_turn("done")           # queues one autopilot turn, which blocks on the lock
    await asyncio.sleep(0.05)
    ap.set(False)                      # switched off while it waits
    fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
    await _drain(eng); await eng.stop()
    assert not any(p.startswith("[autopilot]") for p in getattr(r, "raw", []))
    skipped = [e for e in eng.store.history() if e.get("state") == "skipped"]
    assert skipped and "autopilot is off" in skipped[0]["detail"]


@aio
async def test_autopilot_turn_gets_no_terminal_notice(tmp_path):
    r = FakeRunner(); eng, ap, sent = _apeng(tmp_path, r); ap.set(True)
    eng.terminal = True
    await eng.start(); eng.submit(ap.prompt("x"), "autopilot", internal=True, reply_via="telegram")
    eng.terminal = False; ap.set(False); await _drain(eng); await eng.stop()
    assert not any("You're in the terminal" in s for s in sent)


# --- helpers stage 1 (2026-11-04): helper reports are internal messages; helper steps have their own screens ---

@aio
async def test_helper_submit_source_is_internal_only(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    with pytest.raises(ValueError):
        eng.submit("x", "helper")
    u = eng.submit("[helper helper-1 done: t]\nfound it", "helper", internal=True, reply_via="telegram")
    await _drain(eng)
    assert u["source"] == "helper" and u["reply_via"] == "telegram"
    assert eng.runner.raw == ["[helper] [helper helper-1 done: t]\nfound it"]
    await eng.stop()


@aio
async def test_snapshot_includes_helper_activity(tmp_path):
    eng = _eng(tmp_path, FakeRunner()); await eng.start()
    q = eng.subscribe()
    hid = "helper-01ABC"
    assert eng.activity_snapshot() == {"msg_id": None, "items": [], "helpers": []}
    eng.on_helper_event(hid, {"type": "activity", "id": "t1", "tool": "Read", "title": "Reading x", "state": "running",
                             "parent": None, "helper": False})
    snap = eng.activity_snapshot()
    assert snap["helpers"][0]["id"] == hid and snap["helpers"][0]["items"][0]["title"] == "Reading x"
    assert snap["helpers"][0]["items"][0]["state"] == "running"
    eng.on_helper_event(hid, {"type": "activity", "id": "t1", "state": "done"})
    eng.on_helper_event(hid, {"type": "activity", "id": "stray", "state": "done"})  # start never seen: ignored
    items = eng.activity_snapshot()["helpers"][0]["items"]
    assert [(i["id"], i["state"], i["title"]) for i in items] == [("t1", "done", "Reading x")]
    assert eng.activity_snapshot()["items"] == []  # the turn's own steps are untouched
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    acts = [e for e in evs if e["type"] == "activity"]
    assert [(e["helper_id"], e["msg_id"], e["item"]["state"]) for e in acts] == [(hid, None, "running"), (hid, None, "done")]
    await eng.stop()


@aio
async def test_helper_items_are_dropped_ten_minutes_after_the_end(tmp_path, monkeypatch):
    from navigator.conductor import engine as engine_mod
    clock = {"t": 1000.0}
    calls = []
    monkeypatch.setattr(engine_mod.time, "time", lambda: clock["t"])
    eng = Engine(Store(tmp_path / "c"), FakeRunner(), lock_path=tmp_path / "c" / "turn.lock")
    eng.submit = lambda *a, **k: calls.append((a, k))
    eng.on_helper_event("helper-X", {"type": "activity", "id": "t1", "tool": "Grep", "title": "Searching the code",
                                     "state": "running"})
    eng.on_helper_finish({"id": "helper-X", "title": "t", "state": "done", "result": "r", "ended": 1000.0,
                          "reply_via": None})
    assert calls[0][0] == ("[helper helper-X done: t]\nr", "helper")
    assert [h["id"] for h in eng.activity_snapshot()["helpers"]] == ["helper-X"]
    clock["t"] = 1000.0 + 599
    assert [h["id"] for h in eng.activity_snapshot()["helpers"]] == ["helper-X"]
    clock["t"] = 1000.0 + 601
    assert eng.activity_snapshot()["helpers"] == []


@aio
async def test_turn_sets_helper_via_from_the_message_window(tmp_path):
    from navigator.conductor.helpers import Helpers
    hs = Helpers(tmp_path / "helpers.json", None, lambda *a: None, lambda r: None)
    seen = []
    class Peek(FakeRunner):
        async def __call__(self, prompt, session_id, emit):
            seen.append(hs.current_via)
            return await super().__call__(prompt, session_id, emit)
    eng = Engine(Store(tmp_path / "c"), Peek(), lock_path=tmp_path / "c" / "turn.lock", helpers=hs)
    await eng.start()
    eng.submit("aa", "telegram"); eng.submit("bb", "web")
    eng.submit("cc", "approval", internal=True, reply_via="telegram"); await _drain(eng)
    assert seen == ["telegram", None, "telegram"]
    await eng.stop()


@aio
async def test_helper_report_while_in_terminal_sends_no_terminal_notice(tmp_path):
    sent = []
    eng = Engine(Store(tmp_path / "c"), FakeRunner(), lock_path=tmp_path / "c" / "turn.lock", reply=sent.append)
    await eng.start()
    eng.terminal_joined()
    eng.submit("[helper helper-1 done: t]\nok", "helper", internal=True, reply_via="telegram")
    assert sent == []
    eng.terminal_left([]); await _drain(eng)
    await eng.stop()


# --- 2026-11-08 queue priority: operator lane first, internal reports batched ---
import threading  # noqa: E402


def _rep(n: int, pad: str = "") -> str:
    return f"[helper h{n} done: x]\nr{n}{pad}"


def _qeng(tmp_path, runner, sent=None, **kw):
    return Engine(Store(tmp_path / "c"), runner, lock_path=tmp_path / "c" / "turn.lock",
                  reply=None if sent is None else sent.append, **kw)


def _ids(eng, kind="user"):
    return {e["text"]: e["id"] for e in eng.store.history(limit=1000) if e["kind"] == kind}


def _terminal_states(eng):
    last = {}
    for e in eng.store.history(limit=10000):
        if e.get("kind") == "status":
            last[e["msg_id"]] = e["state"]
    return last


class BoomRunner(FakeRunner):
    async def __call__(self, prompt, session_id, emit):
        self.calls.append((prompt, session_id))
        raise RuntimeError("sdk exploded")


@aio
async def test_t1_operator_jumps_ahead_of_reports(tmp_path):
    for src in ("web", "telegram", "terminal"):
        gate = asyncio.Event(); r = FakeRunner(gate)
        eng = _qeng(tmp_path / src, r) if False else _qeng(tmp_path, r) if src == "web" else None
        if eng is None:
            (tmp_path / src).mkdir()
            eng = _qeng(tmp_path / src, r)
        await eng.start()
        eng.submit("A", "web"); await asyncio.sleep(0.03)
        for i in (1, 2, 3):
            eng.submit(_rep(i), "helper", internal=True)
        eng.submit("hello", src)
        gate.set(); await _drain(eng)
        got = [c[0] for c in r.calls]
        assert got[:2] == ["A", "hello"] and len(got) == 3, got
        assert all(f"[helper h{i} done: x]" in got[2] for i in (1, 2, 3))
        await eng.stop()


@aio
async def test_t2_reports_batched_one_turn(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng = _qeng(tmp_path, r); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    ms = [eng.submit(_rep(i), "helper", internal=True) for i in (1, 2, 3)]
    gate.set(); await _drain(eng)
    assert len(r.calls) == 2
    p = r.calls[1][0]
    assert p.startswith("3 reports arrived together, oldest first. Answer them in one reply.")
    assert p.index("h1 done") < p.index("h2 done") < p.index("h3 done")
    for m in ms:
        assert _status(eng.store, m["id"]) == ["queued", "running", "done"]
    asst = [e for e in eng.store.history() if e["kind"] == "assistant"][1]
    assert asst["reply_to"] == ms[0]["id"] and asst["covers"] == [m["id"] for m in ms]
    assert "covers" not in [e for e in eng.store.history() if e["kind"] == "assistant"][0]
    await eng.stop()


@aio
async def test_t3_caps_items_and_bytes(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _qeng(tmp_path, r, batch_max_items=8); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    for i in range(10):
        eng.submit(_rep(i), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert len(r.calls) == 3
    assert r.calls[1][0].startswith("8 reports") and r.calls[2][0].startswith("2 reports")
    assert "h7 done" in r.calls[1][0] and "h8 done" in r.calls[2][0]
    await eng.stop()


@aio
async def test_t3_byte_cap_and_oversized_first(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate)
    one = len(_rep(1).encode())
    eng = _qeng(tmp_path / "x" if False else tmp_path, r, batch_max_bytes=one * 2 + 2); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    for i in (1, 2, 3):
        eng.submit(_rep(i), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert r.calls[1][0].startswith("2 reports") and "h3 done" in r.calls[2][0] and len(r.calls) == 3
    await eng.stop()
    (tmp_path / "o").mkdir()
    gate2 = asyncio.Event(); r2 = FakeRunner(gate2)
    eng2 = _qeng(tmp_path / "o", r2, batch_max_bytes=10); await eng2.start()
    eng2.submit("A", "web"); await asyncio.sleep(0.03)
    big = eng2.submit(_rep(1, "y" * 100), "helper", internal=True)
    eng2.submit(_rep(2), "helper", internal=True)
    gate2.set(); await _drain(eng2)
    assert len(r2.calls) == 3 and "h1 done" in r2.calls[1][0] and "h2" not in r2.calls[1][0]
    assert _status(eng2.store, big["id"])[-1] == "done"
    await eng2.stop()


@aio
async def test_t4_starvation_age_uses_injected_clock(tmp_path):
    t = [1000.0]
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _qeng(tmp_path, r, clock=lambda: t[0], internal_max_age_s=600); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.submit(_rep(1), "helper", internal=True); eng.submit(_rep(2), "helper", internal=True)
    eng.submit("hello", "web")
    gate.set(); await _drain(eng)
    assert [c[0] for c in r.calls][:2] == ["A", "hello"]
    await eng.stop()
    # aged: the internal batch goes before the waiting operator message
    (tmp_path / "b").mkdir(); t2 = [1000.0]
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _qeng(tmp_path / "b", r, clock=lambda: t2[0], internal_max_age_s=600); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.submit(_rep(1), "helper", internal=True); eng.submit(_rep(2), "helper", internal=True)
    t2[0] += 601
    eng.submit("hello", "web")
    gate.set(); await _drain(eng)
    got = [c[0] for c in r.calls]
    assert got[0] == "A" and got[1].startswith("2 reports") and got[2] == "hello", got
    await eng.stop()


@aio
async def test_t4_no_operator_waiting_internals_run_at_once(tmp_path):
    r = FakeRunner(); eng = _qeng(tmp_path, r); await eng.start()
    eng.submit(_rep(1), "helper", internal=True); await _drain(eng)
    assert len(r.calls) == 1 and "h1 done" in r.calls[0][0]
    await eng.stop()


@aio
async def test_t5_routing(tmp_path):
    async def run(sub, runner=None):
        d = tmp_path / sub; d.mkdir()
        gate = asyncio.Event(); sent = []
        r = runner or FakeRunner(gate)
        eng = _qeng(d, r, sent); await eng.start()
        eng.submit("A", "web"); await asyncio.sleep(0.03)
        return eng, gate, sent
    # windows only: no Telegram send
    eng, gate, sent = await run("w")
    eng.submit(_rep(1), "helper", internal=True); eng.submit(_rep(2), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert sent == []
    await eng.stop()
    # all telegram-bound: one send, no prefix
    eng, gate, sent = await run("t")
    eng.submit(_rep(1), "helper", internal=True, reply_via="telegram")
    eng.submit(_rep(2), "helper", internal=True, reply_via="telegram")
    gate.set(); await _drain(eng)
    assert len(sent) == 1 and not sent[0].startswith("(covers")
    await eng.stop()
    # mixed: one send with the prefix and a history note; model told in the prompt
    eng, gate, sent = await run("m")
    eng.submit(_rep(1), "helper", internal=True, reply_via="telegram")
    eng.submit(_rep(2), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert len(sent) == 1 and sent[0].startswith("(covers 2 reports, some from other windows) ")
    notes = [e["text"] for e in eng.store.history() if e["kind"] == "note"]
    assert "One reply covers 2 reports; sent to the web/terminal windows and to Telegram." in notes
    assert "Your reply goes to: web/terminal windows and Telegram." in eng.runner.calls[1][0]
    await eng.stop()
    # error path routes the same way
    d = tmp_path / "e"; d.mkdir(); sent = []
    gate = asyncio.Event(); r = BoomRunner()
    eng = _qeng(d, r, sent); await eng.start()
    eng.runner = FakeRunner(gate); eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.runner = r
    eng.submit(_rep(1), "helper", internal=True, reply_via="telegram")
    eng.submit(_rep(2), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert len(sent) == 1 and sent[0].startswith("(covers 2 reports, some from other windows) ")
    assert "Stopped:" in sent[0]
    await eng.stop()


@aio
async def test_t6_no_preemption(tmp_path):
    t = [0.0]
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _qeng(tmp_path, r, clock=lambda: t[0], internal_max_age_s=10); await eng.start()
    for i in (1, 2):
        eng.submit(_rep(i), "helper", internal=True)
    await asyncio.sleep(0.03)
    eng.submit("hello", "web"); t[0] += 100
    eng.submit(_rep(3), "helper", internal=True)
    await asyncio.sleep(0.05)
    assert len(r.calls) == 1
    gate.set(); await _drain(eng)
    assert len(r.calls) == 3
    await eng.stop()


@aio
async def test_t6b_cancel_batch(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng = _qeng(tmp_path, r); await eng.start()
    ms = [eng.submit(_rep(i), "helper", internal=True) for i in (1, 2, 3)]
    await asyncio.sleep(0.03)
    assert len(r.calls) == 1 and eng.busy
    op = eng.submit("hello", "web"); m4 = eng.submit(_rep(4), "helper", internal=True)
    assert eng.cancel() is True
    gate.set(); await _drain(eng)
    for m in ms:
        assert _status(eng.store, m["id"]) == ["queued", "running", "error"]
    assert [e["detail"] for e in eng.store.history() if e.get("state") == "error"] == ["cancelled by operator"] * 3
    assert _status(eng.store, op["id"])[-1] == "done" and _status(eng.store, m4["id"])[-1] == "done"
    assert [c[0] for c in r.calls][1] == "hello" and "h4 done" in r.calls[2][0]
    await eng.stop()


class StubAP:
    def __init__(self, why=None):
        self.why = why
    def command(self, text): return None
    def operator_turn(self): pass
    def runnable(self, **kw): return self.why
    def decide(self, outcome, **kw): return ("none", "")
    def started(self): pass


@aio
async def test_t7_autopilot_never_merged_order_kept(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate)
    eng = _qeng(tmp_path, r, autopilot=StubAP()); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.submit(_rep(1), "helper", internal=True)
    eng.submit("[autopilot] item", "autopilot", internal=True)
    eng.submit(_rep(2), "helper", internal=True)
    eng.submit("hello", "web")
    gate.set(); await _drain(eng)
    raw = r.raw
    assert raw[1].endswith("hello")
    assert "h1 done" in raw[2] and "h2" not in raw[2] and "arrived together" not in raw[2]
    assert raw[3].startswith("[autopilot] ") and "h2 done" in raw[4]
    await eng.stop()


@aio
async def test_t7_autopilot_skipped_and_after_turn_sees_both_lanes(tmp_path):
    r = FakeRunner(); eng = _qeng(tmp_path, r, autopilot=StubAP(why="quiet hours")); await eng.start()
    m = eng.submit("[autopilot] item", "autopilot", internal=True); await _drain(eng)
    assert _status(eng.store, m["id"]) == ["queued", "skipped"] and r.calls == []
    await eng.stop()
    # a waiting internal item keeps queue_len > 0, so _after_turn asks autopilot nothing
    (tmp_path / "q").mkdir(); asked = []
    ap = StubAP(); ap.decide = lambda outcome, **kw: asked.append(outcome) or ("none", "")
    gate = asyncio.Event(); eng = _qeng(tmp_path / "q", FakeRunner(gate), autopilot=ap); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.submit(_rep(1), "helper", internal=True)
    gate.set(); await _drain(eng)
    assert asked == ["done"]  # only after the last turn
    await eng.stop()


@aio
async def test_t8_fifo_and_nothing_lost(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng = _qeng(tmp_path, r); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    ids = [eng.submit(t, s, **k)["id"] for t, s, k in [
        ("o1", "web", {}), (_rep(1), "helper", {"internal": True}), ("o2", "telegram", {}),
        (_rep(2), "helper", {"internal": True}), ("o3", "terminal", {})]]
    gate.set(); await _drain(eng)
    assert [c[0] for c in r.calls][:4] == ["A", "o1", "o2", "o3"]
    assert r.calls[4][0].index("h1 done") < r.calls[4][0].index("h2 done")
    states = _terminal_states(eng)
    assert all(states[i] == "done" for i in ids)
    await eng.stop()


@aio
async def test_t9_queue_len_and_queued_detail(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng = _qeng(tmp_path, r)
    assert eng.queue_len == 0
    await eng.start(); assert eng.queue_len == 0
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    for i in (1, 2, 3):
        eng.submit(_rep(i), "helper", internal=True)
    assert eng.queue_len == 3
    hello = eng.submit("hello", "web")
    assert eng.queue_len == 4
    q = {e["msg_id"]: e.get("detail") for e in eng.store.history() if e.get("state") == "queued"}
    assert q[hello["id"]] == 1  # busy + zero operator items ahead; waiting internals go behind it
    gate.set(); await _drain(eng)
    assert eng.queue_len == 0
    await eng.stop()


@aio
async def test_t9_internal_detail_counts_both_lanes(tmp_path):
    gate = asyncio.Event(); eng = _qeng(tmp_path, FakeRunner(gate)); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    eng.submit("o1", "web")
    h = eng.submit(_rep(1), "helper", internal=True)
    q = {e["msg_id"]: e.get("detail") for e in eng.store.history() if e.get("state") == "queued"}
    assert q[h["id"]] == 2
    gate.set(); await _drain(eng); await eng.stop()


@aio
async def test_t11_submit_via_call_soon_threadsafe(tmp_path):
    gate = asyncio.Event(); r = FakeRunner(gate); eng = _qeng(tmp_path, r); await eng.start()
    eng.submit("A", "web"); await asyncio.sleep(0.03)
    loop = asyncio.get_running_loop()

    def work(n):
        for i in range(4):
            if i % 2:
                loop.call_soon_threadsafe(eng.submit, _rep(n * 10 + i), "helper", True)
            else:
                loop.call_soon_threadsafe(eng.submit, f"op{n}{i}", "web")
    ts = [threading.Thread(target=work, args=(n,)) for n in (1, 2, 3)]
    for t in ts: t.start()
    for t in ts: await asyncio.to_thread(t.join)
    await asyncio.sleep(0.05)
    assert len(eng._ops) == 6 and len(eng._internal) == 6
    gate.set()
    await asyncio.wait_for(_drain(eng), 2)
    ops = [c[0] for c in r.calls[1:7]]
    for n in (1, 2, 3):
        assert [o for o in ops if o.startswith(f"op{n}")] == [f"op{n}0", f"op{n}2"]
    batch = " ".join(c[0] for c in r.calls[7:])
    for n in (1, 2, 3):
        assert batch.index(f"h{n*10+1} done") < batch.index(f"h{n*10+3} done")
    assert all(s == "done" for s in _terminal_states(eng).values())
    await eng.stop()
