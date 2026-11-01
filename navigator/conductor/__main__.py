"""python -m navigator.conductor serve [--socket PATH] [--root DIR]
python -m navigator.conductor cards | approve CODE | decline CODE   (the terminal's approval window)"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

from navigator.conductor.server import serve

ROOT = Path("/Users/Manoj/calwbot/logs/conductor")
SOCKET = ROOT / "conductor.sock"


async def cards_cmd(argv: list[str], client: httpx.AsyncClient, out) -> int:
    if argv[0] == "cards":
        cards = (await client.get("/cards")).json()
        if not cards:
            print("no pending approval cards", file=out)
        for c in cards:
            mins = max(0, int((c["expires"] - time.time()) // 60))
            print(f"{c['code']}  ({mins} min left)  {c['summary']}\n        why: {c['reason']}", file=out)
        return 0
    if len(argv) != 2:
        print(__doc__, file=out)
        return 2
    r = await client.post(f"/cards/{argv[1]}/{argv[0]}", json={"via": "terminal"})
    body = r.json()
    if r.status_code != 200:
        print(f"{argv[1]}: {body.get('error')}", file=out)
        return 1
    print(f"{body['code']} {body['state']}: {body['summary']}", file=out)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("cards", "approve", "decline"):
        async def run() -> int:
            transport = httpx.AsyncHTTPTransport(uds=str(SOCKET))
            async with httpx.AsyncClient(transport=transport, base_url="http://conductor") as client:
                return await cards_cmd(argv, client, sys.stdout)
        try:
            return asyncio.run(run())
        except (httpx.ConnectError, FileNotFoundError):
            print("conductor not running", file=sys.stderr)
            return 1
    if not argv or argv[0] != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    opt = lambda name, default: Path(argv[argv.index(name) + 1]) if name in argv else default
    serve(opt("--socket", SOCKET), opt("--root", ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
