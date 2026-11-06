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


# ---- §3b server: no main-turn fallback for an approved card (tests 1-5, 15/16 server level) ----
import httpx  # noqa: E402

from navigator.conductor import helpers as H  # noqa: E402
from navigator.conductor import server  # noqa: E402
from navigator.conductor.cards import Cards  # noqa: E402
from navigator.conductor.engine import Engine  # noqa: E402
from navigator.conductor.store import Store  # noqa: E402
from tests.conductor.test_server import FakeHelpers, _runner  # noqa: E402


class CountingRunner:
    def __init__(self):
        self.n = 0

    async def __call__(self, prompt, sid, emit):
        self.n += 1
        return await _runner(prompt, sid, emit)


async def _approve(tmp_path, *, helpers=None, raise_kw=None, via="telegram", mutate=None):
    cards = Cards(tmp_path / "cards.json")
    runner = CountingRunner(); sent = []
    eng = Engine(Store(tmp_path / "c"), runner, helpers=helpers, reply=sent.append)
    app = server.build_app(eng, cards=cards, helpers=helpers) if helpers else server.build_app(eng, cards=cards)
    await eng.start()
    card, _ = cards.raise_card("Bash", {"command": "git merge x"}, "git merge",
                               **({"cwd": "/r"} if raise_kw is None else raise_kw))
    if mutate:
        mutate(cards)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
            r = await c.post(f"/cards/{card['code']}/approve", json={"via": via})
        await asyncio.sleep(0.1)
    finally:
        await eng.stop()
    return r, card, eng.store.history(limit=1000), runner, sent


def _notes(h):
    return [e["text"] for e in h if e.get("kind") == "note"]


def _no_approval_turn(h):
    return not [e for e in h if e.get("kind") == "user" and e.get("source") == "approval"]


@aio
async def test_no_helpers_approve_starts_no_turn(tmp_path):  # test 1
    r, card, h, runner, sent = await _approve(tmp_path)
    assert r.status_code == 200 and r.json()["state"] == "approved"
    assert _no_approval_turn(h) and runner.n == 0
    notes = _notes(h)
    assert len(notes) == 1 and card["code"] in notes[0] and "cannot be run automatically" in notes[0]


@aio
async def test_note_goes_to_telegram_only_for_telegram(tmp_path):  # test 2
    _, card, _, _, sent = await _approve(tmp_path, via="telegram")
    assert len(sent) == 1 and card["code"] in sent[0]
    _, _, _, _, sent = await _approve(tmp_path / "w", via="web")
    assert sent == []


def _legacy(cards):
    for c in cards._cards:
        c.pop("input", None)


@aio
async def test_legacy_card_without_input(tmp_path):  # test 3
    hp = FakeHelpers()
    r, _, h, runner, _ = await _approve(tmp_path, helpers=hp, mutate=_legacy)
    assert r.status_code == 200 and hp.started == [] and _no_approval_turn(h)
    assert len(_notes(h)) == 1 and "raised before approved cards ran in helpers" in _notes(h)[0]


@aio
async def test_card_without_cwd(tmp_path):  # test 4
    hp = FakeHelpers()
    r, _, h, _, _ = await _approve(tmp_path, helpers=hp, raise_kw={})
    assert r.status_code == 200 and hp.started == [] and _no_approval_turn(h)
    assert len(_notes(h)) == 1 and "no folder recorded" in _notes(h)[0]


@aio
async def test_helper_start_raising(tmp_path, caplog):  # test 5
    class Boom(FakeHelpers):
        def start(self, *a, **k):
            raise RuntimeError("secret-token-xyz")
    with caplog.at_level(logging.WARNING):
        r, card, h, _, _ = await _approve(tmp_path, helpers=Boom())
    assert r.status_code == 200 and r.json()["state"] == "approved" and _no_approval_turn(h)
    notes = _notes(h)
    assert len(notes) == 1 and "RuntimeError" in notes[0] and "secret-token-xyz" not in notes[0]
    assert "RuntimeError" in caplog.text and "secret-token-xyz" not in caplog.text


@aio
async def test_queued_card_helper_tells_operator(tmp_path):  # test 15, server level
    class Queued(FakeHelpers):
        def start(self, *a, **k):
            super().start(*a, **k)
            return {"id": "helper-Q", "title": "t", "state": "queued", "started": 0.0}
    hp = Queued()
    _, card, h, _, _ = await _approve(tmp_path, helpers=hp)
    notes = _notes(h)
    assert len(hp.started) == 1 and len(notes) == 1
    assert card["code"] in notes[0] and "queued" in notes[0] and "ask again" not in notes[0]


@aio
async def test_save_failure_gives_no_ask_again_note(tmp_path, monkeypatch, caplog):  # test 16, server level
    async def runner(task, helper_id, emit, exact=None):
        return "ok"
    hs = H.Helpers(tmp_path / "helpers.json", runner, on_event=lambda *a: None, on_finish=lambda r: None)

    real, seen = hs._save, []

    def boom():
        if not seen:  # only the save inside start()
            seen.append(1)
            raise OSError("disk-secret-xyz")
        real()
    monkeypatch.setattr(hs, "_save", boom)
    with caplog.at_level(logging.WARNING):
        r, _, h, _, _ = await _approve(tmp_path, helpers=hs)
    assert r.status_code == 200 and _no_approval_turn(h)
    assert not [n for n in _notes(h) if "ask again" in n]
    assert "OSError" in caplog.text and "disk-secret-xyz" not in caplog.text
