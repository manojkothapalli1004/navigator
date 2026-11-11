"""Restart card kind, the model-side tool, the decide route, the startup announce, helper isolation."""
import asyncio
import json
import time

import httpx
import pytest

from navigator.conductor import restart, server
from navigator.conductor.cards import CardError, Cards, Declined, MAX_SUMMARY
from navigator.conductor.engine import SOURCES, Engine
from navigator.conductor.store import Store

HELPERS = [{"id": "helper-A", "title": "long lookup", "age": 42.0},
           {"id": "helper-B", "title": "tests", "age": 7.0}]


# ---- cards ----

def test_restart_card_shape(tmp_path):
    cards = Cards(tmp_path / "cards.json")
    card, new = cards.raise_restart_card("conductor", 10, HELPERS, "reload code")
    assert new and card["restart"] is True and card["tool"] == "restart" and card["label"] == "conductor"
    assert card["fingerprint"] == "restart:conductor" and card["delay_s"] == 10
    assert "input" not in card and "cwd" not in card
    s = card["summary"]
    assert "com.calwbot.conductor" in s and "in 10s" in s and "2 helper(s)" in s and "LOST" in s
    assert "helper-A" in s and "interrupted" in s
    none, _ = Cards(tmp_path / "c2.json").raise_restart_card("navigator", 10, [], "r")
    assert "no helpers running" in none["summary"]


def test_restart_card_never_consumable(tmp_path):
    cards = Cards(tmp_path / "cards.json")
    card, _ = cards.raise_restart_card("conductor", 10, [], "r")
    cards.approve(card["code"], "web")
    assert cards.consume("Bash", {"command": "restart conductor"}, cwd="/x") is None
    assert cards.consume("restart", {"label": "conductor"}) is None
    assert cards.consume("restart", {"label": "conductor"}, cwd=None) is None
    assert cards.get(card["code"])["state"] == "approved"


def test_restart_card_dedupe_and_cooldown(tmp_path):
    cards = Cards(tmp_path / "cards.json")
    a, new_a = cards.raise_restart_card("conductor", 10, [], "r")
    b, new_b = cards.raise_restart_card("conductor", 30, HELPERS, "again")
    assert new_a and not new_b and a["code"] == b["code"]
    other, new_o = cards.raise_restart_card("navigator", 10, [], "r")
    assert new_o and other["code"] != a["code"]
    cards.decline(a["code"], "web")
    with pytest.raises(Declined):
        cards.raise_restart_card("conductor", 10, [], "r")


def test_restart_card_shrinks_helper_names_not_label_or_count(tmp_path):
    cards = Cards(tmp_path / "cards.json")
    many = [{"id": f"helper-{i:04d}", "title": "T" * 80, "age": 1.0} for i in range(200)]
    card, _ = cards.raise_restart_card("conductor", 10, many, "r")
    s = card["summary"]
    assert len(s) <= MAX_SUMMARY and "com.calwbot.conductor" in s and "200 helper(s)" in s


# ---- the model-side tool ----

class NoSchedule:
    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(restart, "schedule", lambda *a, **k: self.calls.append((a, k)) or {"ok": True})


def _text(helpers, cards, on_card, args):
    from navigator.conductor.tools import restart_text
    return asyncio.run(restart_text(helpers, cards, on_card, args))


def test_restart_tool_only_raises_a_card(tmp_path, monkeypatch):
    fake = NoSchedule(monkeypatch)
    cards, seen = Cards(tmp_path / "cards.json"), []
    out = _text(None, cards, seen.append, {"label": "conductor", "delay_s": 9999, "reason": "new code"})
    assert "card raised" in out and "nothing happens until it is approved" in out
    (card,) = cards.pending()
    assert card["code"] not in out and card["id"] not in out
    assert card["delay_s"] == 60 and len(seen) == 1
    for _ in range(100):
        _text(None, cards, seen.append, {"label": "conductor", "reason": "again"})
    assert len(cards.pending()) == 1 and len(seen) == 1
    assert fake.calls == []


def test_restart_tool_rejects_bad_label_and_disabled(tmp_path, monkeypatch):
    cards = Cards(tmp_path / "cards.json")
    for bad in ({"label": "rm -rf /", "reason": "x"}, {"reason": "x"}, "oops", None, {"label": ["conductor"]}):
        out = _text(None, cards, lambda c: None, bad)
        assert "label" in out.lower() or "refused" in out.lower()
    assert cards.pending() == []
    monkeypatch.setenv("CONDUCTOR_RESTART_TOOL", "0")
    assert "disabled" in _text(None, cards, lambda c: None, {"label": "conductor", "reason": "x"})
    assert cards.pending() == []


def test_restart_tool_refuses_while_marker_fresh(tmp_path):
    cards = Cards(tmp_path / "cards.json")
    (tmp_path / "restart.json").write_text(json.dumps({"scheduled_at": time.time(), "delay_s": 10}))
    out = _text(None, cards, lambda c: None, {"label": "conductor", "reason": "x"})
    assert "already scheduled" in out and cards.pending() == []


def test_tool_names_and_main_tools_and_helpers_do_not_get_it():
    from navigator.conductor import runner, tools
    from navigator.conductor.helpers import helper_options
    assert "restart" in tools.TOOL_NAMES and "mcp__conductor__restart" in runner.MAIN_TOOLS
    assert "restart" in runner.WINDOWS_PROMPT.lower()
    for exact in (None, {"tool": "Bash", "input": {"command": "ls"}, "cwd": "/Users/Manoj/calwbot"}):
        o = helper_options(None, exact=exact)
        assert not any("conductor" in t for t in o.tools) and not o.mcp_servers.get("conductor")
        assert not any(t.startswith("mcp__conductor__") for t in (o.allowed_tools or []))


# ---- the decide route ----

async def _runner(prompt, sid, emit):
    return "s-1"


class FakeHelpers:
    def __init__(self, snap=()):
        self.started, self.snap = [], list(snap)
    def start(self, *a, **k):
        self.started.append((a, k)); return {"state": "running"}
    def cancel(self, i):
        return False
    def mark_lost(self):
        return []
    def shutdown(self):
        pass
    def snapshot(self):
        return self.snap


def _env(tmp_path, monkeypatch, helpers=None):
    calls = []
    monkeypatch.setattr(restart, "schedule", lambda *a, **k: calls.append((a, k)) or
                        {"ok": True, "at": 1.0, "delay_s": a[1], "old_pid": 1, "helpers": k.get("helpers", [])})
    cards = Cards(tmp_path / "cards.json")
    helpers = helpers or FakeHelpers()
    eng = Engine(Store(tmp_path / "c"), _runner, helpers=helpers)
    return eng, cards, helpers, calls, server.build_app(eng, cards=cards, helpers=helpers)


def _post(app, path, **body):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
            return await c.post(path, json=body)
    return asyncio.run(go())


def test_approve_calls_schedule_once_and_notes(tmp_path, monkeypatch):
    snap = [{"id": "h1", "state": "running", "title": "t", "age": 5.0}]
    eng, cards, helpers, calls, app = _env(tmp_path, monkeypatch, FakeHelpers(snap))
    card, _ = cards.raise_restart_card("conductor", 12, restart.precheck(helpers), "r")

    async def go():
        await eng.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
                assert r.status_code == 200 and r.json()["state"] == "approved"
                assert (await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})).status_code == 409
        finally:
            await eng.stop()
    asyncio.run(go())
    assert len(calls) == 1 and calls[0][0][:2] == ("conductor", 12)
    assert calls[0][1]["card_code"] == card["code"] and calls[0][1]["via"] == "web"
    notes = [e["text"] for e in eng.store.history() if e["kind"] == "note"]
    assert any("Restart scheduled in 12 s" in n and card["code"] in n and "1 helper(s) will be lost" in n
               for n in notes)
    assert helpers.started == []  # a restart card is never fed to a helper


def test_decline_and_dead_cards_never_schedule(tmp_path, monkeypatch):
    eng, cards, helpers, calls, app = _env(tmp_path, monkeypatch)
    a, _ = cards.raise_restart_card("conductor", 10, [], "r")
    assert _post(app, f"/cards/{a['code']}/decline", via="web").json()["state"] == "declined"
    assert _post(app, f"/cards/{a['code']}/approve", via="web").status_code == 409  # declined
    b, _ = cards.raise_restart_card("navigator", 10, [], "r")
    cards._cards[-1]["expires"] = 0  # expired
    assert _post(app, f"/cards/{b['code']}/approve", via="web").status_code == 409
    assert calls == [] and helpers.started == []


@pytest.mark.parametrize("via", SOURCES)
def test_each_via_schedules_once(tmp_path, monkeypatch, via):
    eng, cards, helpers, calls, app = _env(tmp_path, monkeypatch)
    card, _ = cards.raise_restart_card("conductor", 10, [], "r")
    assert _post(app, f"/cards/{card['code']}/approve", via=via).status_code == 200
    assert len(calls) == 1 and calls[0][1]["via"] == via


def test_bad_via_is_422_and_no_call(tmp_path, monkeypatch):
    eng, cards, helpers, calls, app = _env(tmp_path, monkeypatch)
    card, _ = cards.raise_restart_card("conductor", 10, [], "r")
    assert _post(app, f"/cards/{card['code']}/approve", via="fax").status_code == 422
    assert calls == [] and cards.get(card["code"])["state"] == "pending"


def test_schedule_refusal_is_noted(tmp_path, monkeypatch):
    eng, cards, helpers, calls, app = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(restart, "schedule", lambda *a, **k: {"ok": False, "reason": "already scheduled"})
    card, _ = cards.raise_restart_card("conductor", 10, [], "r")

    async def go():
        await eng.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                assert (await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})).status_code == 200
        finally:
            await eng.stop()
    asyncio.run(go())
    assert any("not scheduled" in e["text"].lower() for e in eng.store.history() if e["kind"] == "note")


def test_start_card_helper_ignores_restart_card():
    class E:
        notes = []
        def note(self, *a): self.notes.append(a)
    class H:
        def start(self, *a, **k): raise AssertionError("helper started")
    e = E()
    server.start_card_helper(e, H(), {"code": "ABC", "restart": True, "tool": "restart"}, "web")
    assert e.notes == []


# ---- Engine.start announce ----

def _marker(root, **kw):
    m = {"label": "conductor", "scheduled_at": time.time() - 30, "delay_s": 10, "old_pid": 111,
         "card_code": "X", "via": "web", "helpers_lost": []}
    m.update(kw)
    (root / "restart.json").write_text(json.dumps(m))


def test_engine_start_announces_once(tmp_path):
    root, replies = tmp_path / "c", []
    root.mkdir()
    _marker(root, helpers_lost=[{"id": "helper-A", "title": "stale job"}])

    async def boot():
        eng = Engine(Store(root), _runner, reply=replies.append)
        await eng.start()
        await eng.stop()
        return eng
    eng = asyncio.run(boot())
    notes = [e["text"] for e in eng.store.history() if e["kind"] == "note" and e["text"].startswith("restarted at")]
    assert len(notes) == 1 and "helper-A" in notes[0] and "was pid 111" in notes[0]
    assert len(replies) == 1 and replies[0].startswith("restarted at")
    asyncio.run(boot())
    assert len(replies) == 1  # marker gone: nothing more


def test_engine_start_silent_without_marker(tmp_path):
    root, replies = tmp_path / "c", []

    async def boot():
        eng = Engine(Store(root), _runner, reply=replies.append)
        await eng.start()
        await eng.stop()
        return eng
    eng = asyncio.run(boot())
    assert replies == [] and [e for e in eng.store.history() if e["kind"] == "note"] == []
