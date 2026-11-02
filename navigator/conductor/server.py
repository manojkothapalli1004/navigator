"""HTTP API of the conductor, served only on a 0600 unix socket (no TCP port).

POST /messages {text, source}  GET /history?after=&limit=  GET /events (SSE)  GET /status"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from navigator.conductor.engine import SOURCES, Engine
from navigator.conductor.store import Store

MAX_TEXT = 20000
MAX_TERMINAL_ENTRIES = 500
KEEPALIVE_S = 15.0


async def event_stream(engine: Engine, is_disconnected, keepalive_s: float = KEEPALIVE_S,
                       maxsize: int = 500):
    q = engine.subscribe(maxsize=maxsize)
    try:
        while not await is_disconnected():
            if engine.is_dropped(q) and q.empty():
                return  # fell behind: end the stream so the window reconnects and re-reads /history
            try:
                ev = await asyncio.wait_for(q.get(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                yield {"event": "keepalive", "data": "{}"}
                continue
            yield {"event": ev["type"], "data": json.dumps(ev)}
    finally:
        engine.unsubscribe(q)


def build_app(engine: Engine, manage_engine: bool = False, cards=None) -> Starlette:
    async def post_message(req: Request):
        try:
            body = await req.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=422)
        text, source = (body or {}).get("text"), (body or {}).get("source")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return JSONResponse({"error": f"text must be 1..{MAX_TEXT} characters"}, status_code=422)
        if source not in SOURCES:
            return JSONResponse({"error": f"source must be one of {', '.join(SOURCES)}"}, status_code=422)
        return JSONResponse(engine.submit(text, source), status_code=202)

    async def history(req: Request):
        try:
            limit = max(1, min(int(req.query_params.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        return JSONResponse(engine.store.history(after=req.query_params.get("after"), limit=limit))

    async def events(req: Request):
        return EventSourceResponse(event_stream(engine, req.is_disconnected))

    async def status(req: Request):
        return JSONResponse({"busy": engine.busy, "queue_len": engine.queue_len,
                             "session_id_set": engine.store.session_id is not None,
                             "terminal": engine.terminal,
                             "autopilot": engine.autopilot.public() if engine.autopilot else None})

    async def autopilot(req: Request):
        if engine.autopilot is None:
            return JSONResponse({"error": "autopilot not configured"}, status_code=503)
        try:
            body = await req.json()
        except ValueError:
            body = None
        action = body.get("action") if isinstance(body, dict) else None
        via = body.get("via") if isinstance(body, dict) else None
        if action not in ("on", "off", "status", "quiet_ok") or via not in SOURCES:
            return JSONResponse({"error": f"action must be on/off/status/quiet_ok and via one of {', '.join(SOURCES)}"},
                                status_code=422)
        if action != "status":
            engine.submit(f"autopilot {action.replace('_', ' ')}", via)  # same path as the chat words
        return JSONResponse(engine.autopilot.public())

    async def terminal(req: Request):
        try:
            body = await req.json() or {}
        except ValueError:
            body = {}
        state = body.get("state") if isinstance(body, dict) else None
        if state == "joined":
            engine.terminal_joined()
            return JSONResponse({"terminal": True})
        entries = body.get("entries", []) if state == "left" else None
        if (not isinstance(entries, list) or len(entries) > MAX_TERMINAL_ENTRIES
                or not all(isinstance(e, dict) and e.get("kind") in ("user", "assistant")
                           and isinstance(e.get("text"), str) and len(e["text"]) <= MAX_TEXT for e in entries)):
            return JSONResponse({"error": "state must be joined, or left with up to 500 user/assistant "
                                          f"entries of at most {MAX_TEXT} characters"}, status_code=422)
        return JSONResponse({"imported": engine.terminal_left(entries)})

    async def cancel(req: Request):
        try:
            body = await req.json()
        except ValueError:
            body = None
        via = body.get("via") if isinstance(body, dict) else None
        # via="telegram": Telegram's /stop (the bot answers it); anything else = no via, unchanged
        return JSONResponse({"cancelled": engine.cancel(via="telegram" if via == "telegram" else None)})

    def _public(card: dict) -> dict:
        return {k: v for k, v in card.items() if k != "fingerprint"}

    async def list_cards(req: Request):
        return JSONResponse([_public(c) for c in cards.pending()])

    async def decide(req: Request):
        from navigator.conductor.cards import CardError
        try:
            via = (await req.json() or {}).get("via")
        except ValueError:
            via = None
        if via not in SOURCES:
            return JSONResponse({"error": f"via must be one of {', '.join(SOURCES)}"}, status_code=422)
        action = req.path_params["action"]
        if action not in ("approve", "decline"):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            card = (cards.approve if action == "approve" else cards.decline)(req.path_params["key"], via)
        except CardError as e:
            return JSONResponse({"error": str(e)}, status_code=404 if str(e) == "not found" else 409)
        engine.on_card(card)
        if card["state"] == "approved":
            engine.submit(f"Card {card['code']} was approved by the operator via {via}. Run exactly this "
                          f"now, unchanged: {card['summary']}", "approval", internal=True,
                          reply_via=via)
        return JSONResponse(_public(card))

    @asynccontextmanager
    async def lifespan(app):
        if manage_engine:
            await engine.start()
        try:
            yield
        finally:
            if manage_engine:
                await engine.stop()

    return Starlette(routes=[Route("/messages", post_message, methods=["POST"]),
                             Route("/history", history), Route("/events", events),
                             Route("/status", status),
                             Route("/terminal", terminal, methods=["POST"]),
                             Route("/autopilot", autopilot, methods=["POST"]),
                             Route("/cancel", cancel, methods=["POST"])]
                     + ([Route("/cards", list_cards),
                         Route("/cards/{key}/{action:str}", decide, methods=["POST"])] if cards else []),
                     lifespan=lifespan)


def _ppid(pid: int) -> int | None:
    import subprocess
    out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out) if out.strip().isdigit() else None


def is_descendant(pid: int, ancestor: int, ppid_of=_ppid) -> bool:
    """True when `pid` was started (directly or not) by `ancestor` - a tool the conversation ran."""
    seen = 0
    cur = ppid_of(pid) if pid != ancestor else None
    while cur and cur > 1 and seen < 64:
        if cur == ancestor:
            return True
        cur, seen = ppid_of(cur), seen + 1
    return False


def _peer_pid(sock) -> int | None:
    try:  # macOS: SOL_LOCAL=0, LOCAL_PEERPID=2 (asyncio hands us a TransportSocket wrapper)
        return int(sock.getsockopt(0, 2))
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _guarded_protocol():
    """uvicorn's h11 protocol, but a connection from a process the conversation started (its Bash,
    a script it wrote, curl) is closed before any request is read: the conversation must never
    approve its own card or read the approval state. Same-user processes are otherwise trusted."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class Guarded(H11Protocol):
        def connection_made(self, transport):
            sock = transport.get_extra_info("socket")
            pid = _peer_pid(sock) if sock is not None else None
            if pid is not None and is_descendant(pid, os.getpid()):
                transport.close()
                return
            super().connection_made(transport)
    return Guarded


def serve(socket_path: Path, root: Path, runner=None) -> None:
    """Run the conductor on `socket_path` (replacing a stale file) with state in `root`."""
    import socket
    import uvicorn
    from navigator.conductor import gate, telegram
    from navigator.conductor.cards import Cards
    cards = Cards(Path(root) / "cards.json")
    creds = telegram.load_creds()
    engine_ref: list[Engine] = []
    hook = gate.make_hook(cards, lambda card: engine_ref[0].on_card(card))
    if runner is None:
        from functools import partial
        from navigator.conductor.runner import sdk_turn
        runner = partial(sdk_turn, hook=hook)
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)  # nobody else can even reach the socket's directory
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    # the Telegram post blocks up to 15 s: never inside the turn's event loop; one sender thread
    # keeps cards and replies in the order they happened
    sender = telegram.Sender(creds)
    from navigator.conductor.autopilot import Autopilot
    engine = Engine(Store(root), runner, notify=sender.card, reply=sender.text,
                    autopilot=Autopilot(Path(root) / "autopilot.json"),
                    cards_pending=lambda: bool(cards.pending()))
    engine_ref.append(engine)
    # bound here, not by uvicorn: uvicorn chmods its own unix sockets to 0666
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    uvicorn.Server(uvicorn.Config(build_app(engine, manage_engine=True, cards=cards), log_level="warning",
                                  http=_guarded_protocol())).run(sockets=[sock])
