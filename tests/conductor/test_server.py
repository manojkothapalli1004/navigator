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
        assert s == {"busy": False, "queue_len": 0, "session_id_set": False, "terminal": False, "autopilot": None,
                     "activity": {"msg_id": None, "items": [], "helpers": []}}
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


def test_cancel_passes_via(tmp_path):
    async def go(c, eng):
        seen = []
        eng.cancel = lambda via=None: seen.append(via) or False
        await c.post("/cancel", json={"via": "telegram"})
        await c.post("/cancel")
        await c.post("/cancel", json={"via": "fax"})  # unknown -> treated as no via
        await c.post("/cancel", content=b"not json")
        assert seen == ["telegram", None, None, None]
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


def test_card_approved_via_telegram_submits_reply_via(tmp_path):
    async def go():
        eng, cards, app = _cards_app(tmp_path); await eng.start()
        a, _ = cards.raise_card("Bash", {"command": "git push origin main"}, "git push")
        b, _ = cards.raise_card("Bash", {"command": "git merge x"}, "git merge")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                await c.post(f"/cards/{a['code']}/approve", json={"via": "telegram"})
                await c.post(f"/cards/{b['code']}/approve", json={"via": "web"})
        finally:
            await asyncio.sleep(0.1); await eng.stop()
        users = [e for e in eng.store.history() if e["kind"] == "user"]
        assert [u.get("reply_via") for u in users] == ["telegram", "web"]
    asyncio.run(go())


def test_peer_ancestry():
    parents = {50: 40, 40: 10, 10: 1, 60: 1}
    ppid = parents.get
    assert server.is_descendant(50, 10, ppid) is True
    assert server.is_descendant(60, 10, ppid) is False
    assert server.is_descendant(10, 10, ppid) is False  # the conductor itself (tests, CLI in-process)


def test_socket_refuses_the_conversations_own_tools():
    import subprocess, sys
    d = Path(tempfile.mkdtemp(prefix="cdt", dir="/tmp"))
    sock = d / "c.sock"
    _serve(sock, d / "state")
    # a child of this process (the conductor) = a tool the conversation started
    code = ("import httpx,sys\n"
            "try:\n"
            f"  r=httpx.Client(transport=httpx.HTTPTransport(uds={str(sock)!r})).get('http://c/status')\n"
            "  print(r.status_code)\n"
            "except Exception as e:\n"
            "  print('refused', type(e).__name__)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
    assert out.startswith("refused"), out


def test_terminal_endpoint_and_status(tmp_path):
    async def go(c, eng):
        assert (await c.get("/status")).json()["terminal"] is False
        assert (await c.post("/terminal", json={"state": "joined"})).json() == {"terminal": True}
        assert (await c.get("/status")).json()["terminal"] is True
        r = await c.post("/terminal", json={"state": "left", "entries": [{"kind": "user", "text": "hi"}]})
        assert r.status_code == 200 and r.json() == {"imported": 1}
        assert (await c.get("/status")).json()["terminal"] is False
        bad = [{"state": "left", "entries": [{"kind": "note", "text": "x"}]},
               {"state": "left", "entries": [{"kind": "user", "text": "x"}] * 501},
               {"state": "left", "entries": [{"kind": "user", "text": "x" * 20001}]},
               {"state": "left", "entries": "nope"},
               {"state": "maybe"}]
        for b in bad:
            assert (await c.post("/terminal", json=b)).status_code == 422, b
    asyncio.run(_with_app(tmp_path, go))


# --- 2026-11-02: a refused connection must not log an AttributeError from uvicorn ---

def _refused_protocol(monkeypatch):
    import uvicorn
    from uvicorn.server import ServerState
    monkeypatch.setattr(server, "_peer_pid", lambda sock: 12345)
    monkeypatch.setattr(server, "is_descendant", lambda pid, root: True)
    cfg = uvicorn.Config(app=lambda scope, receive, send: None, log_level="warning")
    cfg.load()
    proto = server._guarded_protocol()(config=cfg, server_state=ServerState(), app_state={})

    class T:
        closed = False
        def get_extra_info(self, name, default=None):
            return object() if name == "socket" else default
        def close(self):
            self.closed = True
    t = T()
    proto.connection_made(t)
    return proto, t


def test_refused_connection_closes_quietly(monkeypatch):
    async def go():  # uvicorn's protocol needs a running loop
        proto, t = _refused_protocol(monkeypatch)
        assert t.closed
        proto.data_received(b"GET /cards HTTP/1.1\r\nHost: c\r\n\r\n")  # must not be parsed or raise
        proto.connection_lost(None)  # asyncio calls this after close(): must not raise
    asyncio.run(go())


def test_autopilot_endpoint_and_status(tmp_path):
    from navigator.conductor.autopilot import Autopilot
    (tmp_path / "plan.md").write_text("- [ ] x\n")
    ap = Autopilot(tmp_path / "ap.json", tmp_path / "plan.md")
    eng = Engine(Store(tmp_path / "c"), _runner, autopilot=ap)
    app = server.build_app(eng)

    async def go():
        await eng.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                assert (await c.get("/status")).json()["autopilot"]["on"] is False
                r = await c.post("/autopilot", json={"action": "on", "via": "terminal"})
                assert r.status_code == 200 and r.json()["on"] is True
                assert (await c.post("/autopilot", json={"action": "fly"})).status_code == 422
                assert (await c.post("/autopilot", json={"action": "off", "via": "terminal"})).json()["on"] is False
        finally:
            await eng.stop()
    asyncio.run(go())


# ---- helpers stage 1 (2026-11-04): an approved card runs in a helper, never in the main turn ----

class FakeHelpers:
    def __init__(self):
        self.started, self.cancelled = [], []
    def start(self, title, task, *, exact=None, via=None):
        self.started.append({"title": title, "task": task, "exact": exact, "via": via})
        return {"id": "helper-F", "title": title, "state": "running", "started": 0.0}
    def cancel(self, helper_id):
        self.cancelled.append(helper_id)
        return helper_id == "helper-F"
    def mark_lost(self):
        return []
    def shutdown(self):
        pass
    def snapshot(self):
        return []


def test_approved_card_runs_in_helper(tmp_path):
    async def go():
        from navigator.conductor.cards import Cards
        cards = Cards(tmp_path / "cards.json")
        helpers = FakeHelpers()
        eng = Engine(Store(tmp_path / "c"), _runner, helpers=helpers)
        app = server.build_app(eng, cards=cards, helpers=helpers)
        submitted = []
        real_submit = eng.submit
        eng.submit = lambda *a, **k: submitted.append((a, k)) or real_submit(*a, **k)
        await eng.start()
        a, _ = cards.raise_card("Bash", {"command": "git push origin main", "description": "Push"}, "git push",
                                cwd="/Users/Manoj/calwbot/navigator")
        b, _ = cards.raise_card("Bash", {"command": "git merge x"}, "git merge", cwd="/r")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                r = await c.post(f"/cards/{a['code']}/approve", json={"via": "telegram"})
                assert r.status_code == 200 and r.json()["state"] == "approved"
                assert "input" not in r.json() and "cwd" not in r.json()
                assert (await c.post(f"/cards/{b['code']}/decline", json={"via": "web"})).json()["state"] == "declined"
                assert (await c.post("/helpers/helper-F/cancel")).json() == {"cancelled": True}
                assert (await c.post("/helpers/helper-Q/cancel")).json() == {"cancelled": False}
        finally:
            await asyncio.sleep(0.05); await eng.stop()
        assert len(helpers.started) == 1
        s = helpers.started[0]
        assert s["exact"] == {"tool": "Bash", "input": {"command": "git push origin main", "description": "Push"},
                              "cwd": "/Users/Manoj/calwbot/navigator"} and s["via"] == "telegram"
        assert s["title"] == f"approved card {a['code']}" and a["code"] in s["task"]
        assert "git push origin main" in s["task"] and "/Users/Manoj/calwbot/navigator" in s["task"]
        assert not any(a_[1] == "approval" for a_, _ in submitted)
        assert helpers.cancelled == ["helper-F", "helper-Q"]
        assert [e["state"] for e in eng.store.history() if e["kind"] == "card"] == ["approved", "declined"]
        assert all("input" not in e and "cwd" not in e for e in eng.store.history() if e["kind"] == "card")
    asyncio.run(go())


def test_helpers_cancel_route_absent_without_helpers(tmp_path):
    async def go(c, eng):
        assert (await c.post("/helpers/helper-F/cancel")).status_code == 404
    asyncio.run(_with_app(tmp_path, go))


# ---- build cards: Approve/Decline on a build that waits for the operator ----
SPEC = "x" * 500


class _BCfg:
    tier_c_paths = ()
    tier_floor = "A"


class _BClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _build_env(tmp_path, monkeypatch, with_builds=True):
    from navigator.clients.act_client import FakeACTClient
    from navigator.conductor import builds as B
    from navigator.conductor.cards import Cards
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: _BCfg())
    clock = _BClock()
    cards = Cards(tmp_path / "cards.json", clock=clock)
    act = FakeACTClient()
    bs = B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda r: "s")
    eng = Engine(Store(tmp_path / "c"), _runner, builds=bs)
    bs.on_needs_approval = lambda rec: cards.raise_build_card(
        rec["proposal_id"], rec["project"], rec["title"], rec["files"], rec["approval_reason"])
    helpers = FakeHelpers()
    app = server.build_app(eng, cards=cards, helpers=helpers, builds=bs if with_builds else None)
    rec = bs.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")
    card = cards.pending()[0]
    return eng, cards, act, bs, helpers, app, clock, rec["proposal_id"], card


def _run_build(tmp_path, monkeypatch, body, **kw):
    async def go():
        env = _build_env(tmp_path, monkeypatch, **kw)
        eng = env[0]
        await eng.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env[5]), base_url="http://c") as c:
                await body(c, *env)
        finally:
            await asyncio.sleep(0.05)
            await eng.stop()
    asyncio.run(go())


def _approved(act, pid):
    from navigator.contracts.enums import ProposalStatus
    return act.proposals[pid].status == ProposalStatus.approved


def test_build_card_approve_sets_proposal_approved_no_helper_no_turn(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        eng.on_card(card)
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 200 and r.json()["state"] == "approved" and r.json()["build"] is True
        assert _approved(act, pid) and helpers.started == []
        h = eng.store.history()
        assert not [e for e in h if e.get("source") == "approval"]
        cs = [e for e in h if e["kind"] == "card"]
        assert [e["state"] for e in cs] == ["pending", "approved"] and all(e["build"] is True for e in cs)
    _run_build(tmp_path, monkeypatch, body)


def test_build_card_decline_sets_declined(tmp_path, monkeypatch):
    from navigator.contracts.enums import ProposalStatus
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        r = await c.post(f"/cards/{card['id']}/decline", json={"via": "telegram"})
        assert r.status_code == 200 and r.json()["state"] == "declined"
        assert act.proposals[pid].status == ProposalStatus.declined and helpers.started == []
    _run_build(tmp_path, monkeypatch, body)


def test_build_card_expiry_leaves_proposal_proposed(tmp_path, monkeypatch):
    from navigator.contracts.enums import ProposalStatus
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        clock.t += 4000
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 409 and r.json()["error"] == "expired"
        assert act.proposals[pid].status == ProposalStatus.proposed
        bs.poll()
        assert bs.snapshot()[0]["state"] == "filed" and len(cards._cards) == 1
        assert cards.raise_build_card(pid, "navigator", "Code", ["navigator/a.py"], "r")[1] is False
        bs.decide(pid, True)  # what Telegram "approve N" amounts to
        assert _approved(act, pid)
    _run_build(tmp_path, monkeypatch, body)


def test_build_card_act_down_keeps_card_pending(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        real = act.set_proposal_status

        def down(*a):
            raise httpx.ConnectError("down")
        act.set_proposal_status = down
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 502 and "ACT is not reachable" in r.json()["error"]
        assert cards.get(card["code"])["state"] == "pending"
        act.set_proposal_status = real
        assert (await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})).status_code == 200
        assert _approved(act, pid)
    _run_build(tmp_path, monkeypatch, body)


def test_build_card_proposal_moved_on(tmp_path, monkeypatch):
    from navigator.contracts.enums import ProposalStatus
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        act.proposals[pid].status = ProposalStatus.declined
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 409 and r.json()["error"] == "proposal already declined"
        assert cards.get(card["code"])["state"] == "pending"
    _run_build(tmp_path, monkeypatch, body)


def test_build_card_with_builds_off_is_refused(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 503 and helpers.started == []
        assert not [e for e in eng.store.history() if e.get("source") == "approval"]
        assert cards.get(card["code"])["state"] == "pending"
    _run_build(tmp_path, monkeypatch, body, with_builds=False)


def test_build_card_double_tap(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        assert (await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})).status_code == 200
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 409 and "already approved" in r.json()["error"]
        bs.decide(pid, True)  # idempotent
        assert _approved(act, pid)
    _run_build(tmp_path, monkeypatch, body)


def test_model_cannot_approve(tmp_path, monkeypatch):
    from navigator.conductor import tools
    from navigator.contracts.enums import ProposalStatus
    assert tools.TOOL_NAMES == ["delegate", "helpers", "cancel_helper"]
    tools.make_conductor_server(FakeHelpers(), None)  # builds with the array schema; exposes no approve tool

    async def body(c, eng, cards, act, bs, helpers, app, clock, pid, card):
        calls = []
        real = act.set_proposal_status
        act.set_proposal_status = lambda p, s: calls.append(s) or real(p, s)
        r = await tools.delegate_text(helpers, {"kind": "build", "project": "navigator", "title": "Other",
                                                "task": SPEC, "files": ["navigator/b.py"]}, bs)
        assert ProposalStatus.approved not in calls
        newest = cards.pending()[-1]
        assert newest["code"] not in r and newest["id"] not in r
    _run_build(tmp_path, monkeypatch, body)


def test_telegram_notified_once_for_a_pending_build_card(tmp_path):
    from navigator.conductor import telegram
    from navigator.conductor.cards import Cards
    sent = []
    eng = Engine(Store(tmp_path / "c"), _runner, notify=sent.append)
    card, _ = Cards(tmp_path / "k.json").raise_build_card("p1", "navigator", "T", ["a.py"], "why")
    eng.on_card(card)
    assert sent == [card]
    text = telegram.card_text(card)
    assert card["summary"] in text and f"ok {card['code']}" in text
    eng.on_card({**card, "state": "approved"})
    assert len(sent) == 1


def test_serve_wiring_raises_one_card_and_one_history_entry(tmp_path, monkeypatch):
    # what serve() installs as builds.on_needs_approval: raise once, tell the engine once
    from navigator.conductor.cards import Cards
    cards = Cards(tmp_path / "k.json")
    eng = Engine(Store(tmp_path / "c"), _runner)
    cb = server.make_build_card_callback(cards, [eng])
    rec = {"proposal_id": "p1", "project": "navigator", "title": "T", "files": ["a.py"],
           "approval_reason": "why"}
    cb(rec)
    cb(rec)
    assert len(cards.pending()) == 1
    assert len([e for e in eng.store.history() if e["kind"] == "card"]) == 1
