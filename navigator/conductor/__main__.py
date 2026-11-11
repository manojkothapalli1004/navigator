"""python -m navigator.conductor serve [--socket PATH] [--root DIR]
python -m navigator.conductor cards | approve CODE | decline CODE   (the terminal's approval window)
python -m navigator.conductor join   (open Claude in this terminal on the shared conversation)
python -m navigator.conductor ledger [--since 1d|6h|YYYY-MM-DD]   (what helpers and builds did; reads the local file)"""
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


async def autopilot_cmd(argv: list[str], client: httpx.AsyncClient, out) -> int:
    """autopilot on | off | status | quiet-ok — the terminal's switch (same as the chat words)."""
    action = (argv[1] if len(argv) > 1 else "status").replace("-", "_")
    r = await client.post("/autopilot", json={"action": action, "via": "terminal"})
    body = r.json()
    if r.status_code != 200:
        print(f"autopilot: {body.get('error')}", file=out)
        return 1
    print(f"autopilot {'on' if body['on'] else 'off'} — {body['consecutive']}/{body['max_consecutive']} in a row, "
          f"{body['today']}/{body['max_per_day']} today; plan: {body['plan']}", file=out)
    return 0


def _since(v: str) -> float | None:
    """Seconds-ago like 6h / 2d / 30m, or a YYYY-MM-DD date; None when it is neither."""
    import re
    m = re.fullmatch(r"(\d+)([mhd])", v)
    if m:
        return time.time() - int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]
    try:
        return time.mktime(time.strptime(v, "%Y-%m-%d"))
    except ValueError:
        return None


def ledger_cmd(argv: list[str], out, err) -> int:
    from navigator import ledger
    since = None
    if "--since" in argv:
        i = argv.index("--since")
        since = _since(argv[i + 1]) if i + 1 < len(argv) else None
        if since is None:
            print("ledger: --since takes 1d, 6h, 30m or YYYY-MM-DD", file=err)
            return 2
    rows = ledger.read(since)
    if not rows:
        none_yet = not ledger.PATH.exists() and not Path(str(ledger.PATH) + ".1").exists()
        print("no ledger yet" if none_yet else "no ledger entries in that range", file=out)
        return 0
    print(ledger.table(rows[-50:]), file=out)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ledger"]:
        return ledger_cmd(argv, sys.stdout, sys.stderr)
    if argv[:1] == ["autopilot"]:
        async def run_ap() -> int:
            transport = httpx.AsyncHTTPTransport(uds=str(SOCKET))
            async with httpx.AsyncClient(transport=transport, base_url="http://conductor") as client:
                return await autopilot_cmd(argv, client, sys.stdout)
        try:
            return asyncio.run(run_ap())
        except (httpx.ConnectError, FileNotFoundError):
            print("conductor not running", file=sys.stderr)
            return 1
    if argv[:1] == ["join"]:
        from navigator.conductor.join import join
        return join(ROOT, SOCKET)
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
