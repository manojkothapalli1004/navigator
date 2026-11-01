import asyncio
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from navigator.conductor import server
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store


async def _runner(prompt, sid, emit):
    emit({"type": "delta", "text": "pong"})
    return "s-1"


def _client(tmp_path):
    eng = Engine(Store(tmp_path / "c"), _runner)
    app = server.build_app(eng)
    return eng, app


async def _with_app(tmp_path, fn):
    eng, app = _client(tmp_path)
    await eng.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
            return await fn(c, eng)
    finally:
        await eng.stop()


def test_post_message_202_and_validation(tmp_path):
    async def go(c, eng):
        r = await c.post("/messages", json={"text": "ping", "source": "web"})
        assert r.status_code == 202 and r.json()["kind"] == "user"
        for bad in ({"text": "", "source": "web"}, {"text": "x", "source": "email"},
                    {"text": "x" * 20001, "source": "web"}, {"source": "web"}):
            assert (await c.post("/messages", json=bad)).status_code == 422
        assert (await c.post("/messages", content=b"not json")).status_code == 422
    asyncio.run(_with_app(tmp_path, go))


def test_history_endpoint(tmp_path):
    async def go(c, eng):
        r = await c.post("/messages", json={"text": "ping", "source": "web"})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not eng.busy and eng.queue_len == 0:
                break
        h = (await c.get("/history")).json()
        assert [e["kind"] for e in h if e["kind"] in ("user", "assistant")] == ["user", "assistant"]
        after = (await c.get("/history", params={"after": r.json()["id"]})).json()
        assert all(e["id"] != r.json()["id"] for e in after)
    asyncio.run(_with_app(tmp_path, go))


def test_status(tmp_path):
    async def go(c, eng):
        s = (await c.get("/status")).json()
        assert s == {"busy": False, "queue_len": 0, "session_id_set": False}
    asyncio.run(_with_app(tmp_path, go))


def test_events_stream_entry_and_delta(tmp_path):
    async def go():
        eng, _ = _client(tmp_path)
        await eng.start()
        disconnected = {"v": False}
        async def is_disc():
            return disconnected["v"]
        gen = server.event_stream(eng, is_disc, keepalive_s=0.05)
        eng.submit("ping", "web")
        seen = []
        async for ev in gen:
            seen.append(ev)
            if ev.get("event") == "delta" or len(seen) > 20:
                disconnected["v"] = True
        assert any(ev.get("event") == "entry" for ev in seen)
        assert any(ev.get("event") == "delta" for ev in seen)
        assert eng._subs == []  # unsubscribed on disconnect
        await eng.stop()
    asyncio.run(go())


def _serve(sock: Path, root: Path):
    t = threading.Thread(target=server.serve, args=(sock, root), kwargs={"runner": _runner}, daemon=True)
    t.start()
    for _ in range(100):
        if sock.exists():
            try:
                httpx.Client(transport=httpx.HTTPTransport(uds=str(sock))).get("http://c/status")
                return t
            except httpx.TransportError:
                pass
        time.sleep(0.05)
    raise AssertionError("server did not come up")


def test_socket_mode_0600_and_stale_socket_replaced():
    d = Path(tempfile.mkdtemp(prefix="cdt", dir="/tmp"))
    sock = d / "c.sock"
    sock.write_text("stale")  # a leftover file from a crash
    old = os.umask(0o022)
    try:
        _serve(sock, d / "state")
    finally:
        os.umask(old)
    assert stat.S_ISSOCK(sock.stat().st_mode)
    assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    r = httpx.Client(transport=httpx.HTTPTransport(uds=str(sock))).post(
        "http://c/messages", json={"text": "hi", "source": "terminal"})
    assert r.status_code == 202


def test_event_stream_ends_when_subscriber_dropped(tmp_path):
    async def go():
        eng, _ = _client(tmp_path)
        await eng.start()
        async def never():
            return False
        gen = server.event_stream(eng, never, keepalive_s=0.05, maxsize=1)
        first = await gen.__anext__()  # subscribes; first keepalive
        for i in range(3):
            eng.submit(f"m{i}", "web")
        seen = 0
        async for _ in gen:
            seen += 1
            assert seen < 50, "stream kept going after the subscriber was dropped"
        assert eng._subs == []
        await eng.stop()
    asyncio.run(asyncio.wait_for(go(), 10))


def test_post_cancel_endpoint(tmp_path):
    async def go(c, eng):
        r = await c.post("/cancel")
        assert r.status_code == 200 and r.json() == {"cancelled": False}
    asyncio.run(_with_app(tmp_path, go))


def _cards_app(tmp_path):
    from navigator.conductor.cards import Cards
    cards = Cards(tmp_path / "cards.json")
    eng = Engine(Store(tmp_path / "c"), _runner)
    return eng, cards, server.build_app(eng, cards=cards)


def test_cards_endpoints_approve_decline_404_409(tmp_path):
    async def go():
        eng, cards, app = _cards_app(tmp_path); await eng.start()
        a, _ = cards.raise_card("Bash", {"command": "git push"}, "git push")
        b, _ = cards.raise_card("Bash", {"command": "git merge x"}, "git merge")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                assert [x["code"] for x in (await c.get("/cards")).json()] == [a["code"], b["code"]]
                assert "fingerprint" not in (await c.get("/cards")).json()[0]
                r = await c.post(f"/cards/{a['code']}/approve", json={"via": "web"})
                assert r.status_code == 200 and r.json()["state"] == "approved"
                assert (await c.post(f"/cards/{a['code']}/approve", json={"via": "web"})).status_code == 409
                assert (await c.post("/cards/NOPE22/approve", json={"via": "web"})).status_code == 404
                assert (await c.post(f"/cards/{b['id']}/decline", json={"via": "telegram"})).json()["state"] == "declined"
                assert (await c.post(f"/cards/{b['id']}/decline", json={"via": "fax"})).status_code == 422
                c2, _ = cards.raise_card("Bash", {"command": "git rebase x"}, "git rebase")
                assert (await c.post(f"/cards/{c2['code']}/foo", json={"via": "web"})).status_code == 404
                assert cards.pending()[-1]["state"] == "pending"
        finally:
            await eng.stop()
    asyncio.run(go())


def test_approve_submits_internal_message(tmp_path):
    async def go():
        eng, cards, app = _cards_app(tmp_path); await eng.start()
        a, _ = cards.raise_card("Bash", {"command": "git push origin main"}, "git push")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                await c.post(f"/cards/{a['code']}/approve", json={"via": "telegram"})
        finally:
            await asyncio.sleep(0.1); await eng.stop()
        users = [e for e in eng.store.history() if e["kind"] == "user"]
        assert users[-1]["source"] == "approval"
        assert "git push origin main" in users[-1]["text"] and a["code"] in users[-1]["text"]
        assert [e["state"] for e in eng.store.history() if e["kind"] == "card"] == ["approved"]
    asyncio.run(go())
