import asyncio
import io

import httpx

from navigator.conductor import __main__ as cli
from navigator.conductor import server
from navigator.conductor.cards import Cards
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store


async def _runner(prompt, sid, emit):
    return "s"


def test_cli_approve_and_list(tmp_path):
    async def go():
        cards = Cards(tmp_path / "cards.json")
        eng = Engine(Store(tmp_path / "c"), _runner); await eng.start()
        app = server.build_app(eng, cards=cards)
        card, _ = cards.raise_card("Bash", {"command": "git push origin main"}, "git push")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")
        out = io.StringIO()
        try:
            assert await cli.cards_cmd(["cards"], client, out) == 0
            assert card["code"] in out.getvalue() and "git push origin main" in out.getvalue()
            assert await cli.cards_cmd(["approve", card["code"].lower()], client, out) == 0
            assert "approved" in out.getvalue()
            assert await cli.cards_cmd(["approve", card["code"]], client, out) == 1
            assert "already approved" in out.getvalue()
        finally:
            await client.aclose(); await eng.stop()
    asyncio.run(go())


def test_terminal_autopilot_switch(tmp_path):
    from navigator.conductor.autopilot import Autopilot
    (tmp_path / "plan.md").write_text("- [ ] x\n")
    ap = Autopilot(tmp_path / "ap.json", tmp_path / "plan.md")
    eng = Engine(Store(tmp_path / "c"), lambda *a: None, autopilot=ap)
    app = server.build_app(eng)

    async def go():
        await eng.start()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")
        try:
            out = io.StringIO()
            assert await cli.autopilot_cmd(["autopilot", "on"], client, out) == 0
            assert ap.on and out.getvalue().startswith("autopilot on")
            assert await cli.autopilot_cmd(["autopilot", "off"], client, io.StringIO()) == 0 and not ap.on
            bad = io.StringIO()
            assert await cli.autopilot_cmd(["autopilot", "fly"], client, bad) == 1 and "action must be" in bad.getvalue()
            notes = [e["text"] for e in eng.store.history() if e["kind"] == "note"]
            assert any("(via terminal)" in n for n in notes)
        finally:
            await client.aclose(); await eng.stop()
    asyncio.run(go())


def test_cli_ledger_prints_table(tmp_path, monkeypatch, capsys):
    import time
    from navigator import ledger
    p = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(ledger, "PATH", p)
    assert cli.main(["ledger"]) == 0
    assert "no ledger yet" in capsys.readouterr().out
    now = time.time()
    ledger.append({"v": 1, "kind": "helper", "id": "helper-1", "title": "recent", "state": "done",
                   "started": now - 60, "ended": now - 5, "tools": {"Read": 1}}, p)
    ledger.append({"v": 1, "kind": "helper", "id": "helper-0", "title": "ancient", "state": "done",
                   "started": now - 9 * 86400, "ended": now - 9 * 86400 + 3, "tools": {}}, p)
    assert cli.main(["ledger"]) == 0
    out = capsys.readouterr().out
    assert "recent" in out and "ancient" in out
    assert cli.main(["ledger", "--since", "1d"]) == 0
    out = capsys.readouterr().out
    assert "recent" in out and "ancient" not in out
    assert cli.main(["ledger", "--since", "yesterday-ish"]) == 2
