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
