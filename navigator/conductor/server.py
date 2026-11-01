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


def build_app(engine: Engine, manage_engine: bool = False) -> Starlette:
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
                             "session_id_set": engine.store.session_id is not None})

    async def cancel(req: Request):
        return JSONResponse({"cancelled": engine.cancel()})

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
                             Route("/cancel", cancel, methods=["POST"])], lifespan=lifespan)


def serve(socket_path: Path, root: Path, runner=None) -> None:
    """Run the conductor on `socket_path` (replacing a stale file) with state in `root`."""
    import socket
    import uvicorn
    if runner is None:
        from navigator.conductor.runner import sdk_turn as runner
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)  # nobody else can even reach the socket's directory
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    engine = Engine(Store(root), runner)
    # bound here, not by uvicorn: uvicorn chmods its own unix sockets to 0666
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    uvicorn.Server(uvicorn.Config(build_app(engine, manage_engine=True), log_level="warning")).run(sockets=[sock])
