"""HTTP API of the conductor, served only on a 0600 unix socket (no TCP port).

POST /messages {text, source}  POST /attachments {name, data(base64), text, source}  GET /history?after=&limit=  GET /events (SSE)  GET /status"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from navigator.conductor import attachments
from navigator.conductor.engine import SOURCES, Engine
from navigator.conductor.store import Store

_log = logging.getLogger(__name__)
MAX_TEXT = 20000
MAX_TERMINAL_ENTRIES = 500
KEEPALIVE_S = 15.0
# the main turn has no gated tools any more: an approved card is run by a helper that may make
# only this one call (helpers.sdk_helper with `exact`), and its card hook consumes the approval
CARD_TASK = ("Card {0} was approved by the operator. Issue exactly this one tool call, unchanged, then "
             "report its result in plain words: tool {1}, input {2}, folder {3}.")


def _public(card: dict) -> dict:
    # fingerprint, input, cwd stay inside: the summary is what the operator approves
    return {k: v for k, v in card.items() if k not in ("fingerprint", "input", "cwd")}


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


async def _decide_build(cards, builds, lock, bc: dict, action: str):
    """Approve/Decline on a build card: ACT first, the card second. None = the proposal is updated."""
    from navigator.conductor.builds import BuildRefused
    if bc["state"] != "pending":
        return JSONResponse({"error": f"already {bc['state']}"}, status_code=409)
    if cards.clock() >= bc["expires"]:
        return JSONResponse({"error": "expired"}, status_code=409)
    async with lock:  # one ACT status change at a time
        try:
            await asyncio.to_thread(builds.decide, bc["proposal_id"], action == "approve")
        except BuildRefused as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except Exception:
            return JSONResponse({"error": "ACT is not reachable; the card stays pending"}, status_code=502)
    return None


def make_build_card_callback(cards, engine_ref: list):
    """Builds.on_needs_approval: runs on the event loop; one card per proposal, chat + Telegram told once."""
    def _raise_build_card(rec: dict) -> None:
        card, new = cards.raise_build_card(rec["proposal_id"], rec["project"], rec["title"], rec["files"],
                                           rec.get("approval_reason") or "needs your approval",
                                           est_cost_usd=rec.get("est_cost_usd"))
        if new:
            engine_ref[0].on_card(card)
    return _raise_build_card


def start_card_helper(engine, helpers, card: dict, via: str) -> None:
    """An approved card runs in a helper or not at all: never a main-conversation turn."""
    code = card["code"]
    why = None
    if helpers is None:
        why = "no helpers"
    elif not isinstance(card.get("input"), dict):
        why = "raised before approved cards ran in helpers"
    elif not (isinstance(card.get("cwd"), str) and card["cwd"]):
        why = "no folder recorded"
    elif not isinstance(card.get("tool"), str):
        why = "no tool recorded"
    if why:
        engine.note(f"Card {code} was approved but cannot be run automatically ({why}); ask for it again.", via)
        return
    try:
        out = helpers.start(title=f"approved card {code}",
                            task=CARD_TASK.format(code, card["tool"], json.dumps(card["input"]), card["cwd"]),
                            exact={"tool": card["tool"], "input": card["input"], "cwd": card["cwd"]}, via=via)
    except Exception as e:
        name = type(e).__name__
        _log.warning("conductor.card_helper_failed: %s", name)
        engine.note(f"Card {code} was approved but its helper could not start ({name}); ask for it again.", via)
        return
    if isinstance(out, dict) and out.get("state") == "queued":
        engine.note(f"Card {code} was approved; it is queued behind other helpers and will run when one ends.", via)


def build_app(engine: Engine, manage_engine: bool = False, cards=None, helpers=None, builds=None,
              attachments_dir=None) -> Starlette:
    build_lock = asyncio.Lock()

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

    def _store_attachment(name: str, data_b64: str, adir):
        """Heavy part, runs in a worker thread: decode, check, save. Returns (display, path, copy)."""
        try:
            data = base64.b64decode(data_b64, validate=True)
        except ValueError:
            raise attachments.AttachmentError(422, "data must be base64") from None
        if len(data) > attachments.MAX_BYTES:
            raise attachments.AttachmentError(413, "file is over 10 MB")
        if not data:
            raise attachments.AttachmentError(422, "file is empty")
        stem, ext = attachments.sanitize_name(name)
        text = attachments.check_content(ext, data)
        stored = attachments.stored_name(stem, ext)
        path, copy = attachments.save(adir, stored, data, text=text if ext == ".docx" else None)
        return f"{stem}{ext}", path, copy

    async def post_attachment(req: Request):
        if attachments_dir is None:
            return JSONResponse({"error": "attachments are not configured"}, status_code=503)
        limit = attachments.MAX_BYTES * 4 // 3 + 100_000
        declared = req.headers.get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                return JSONResponse({"error": "bad Content-Length"}, status_code=422)
            if too_big:
                return JSONResponse({"error": "file is over 10 MB"}, status_code=413)
        raw = bytearray()
        async for chunk in req.stream():
            raw += chunk
            if len(raw) > limit:
                return JSONResponse({"error": "file is over 10 MB"}, status_code=413)
        try:
            body = json.loads(bytes(raw))
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=422)
        name, data, caption, source = body.get("name"), body.get("data"), body.get("text", ""), body.get("source")
        if not isinstance(name, str) or not isinstance(data, str) or not isinstance(caption, str):
            return JSONResponse({"error": "name, data and text must be strings"}, status_code=422)
        if source not in SOURCES:
            return JSONResponse({"error": f"source must be one of {', '.join(SOURCES)}"}, status_code=422)
        caption = caption.strip()
        if len(caption) > MAX_TEXT - 1500:
            return JSONResponse({"error": f"text must be at most {MAX_TEXT - 1500} characters"},
                                status_code=422)
        if len(data) > attachments.MAX_BYTES * 4 // 3 + 4:
            return JSONResponse({"error": "file is over 10 MB"}, status_code=413)
        try:
            display, path, copy = await asyncio.to_thread(_store_attachment, name, data, attachments_dir)
        except attachments.AttachmentError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        except Exception:
            return JSONResponse({"error": "could not read that file"}, status_code=422)
        line = attachments.note(display, path, copy)
        try:
            entry = engine.submit(f"{caption}\n\n{line}" if caption else line, source)
        except Exception:
            def _undo():
                for p in (path, copy):
                    if p is not None:
                        p.unlink(missing_ok=True)
            await asyncio.to_thread(_undo)
            return JSONResponse({"error": "could not queue that file"}, status_code=500)
        return JSONResponse(entry, status_code=202)

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
                             "autopilot": engine.autopilot.public() if engine.autopilot else None,
                             # the latest turn's steps: crew screens for a window opened mid-turn
                             "activity": engine.activity_snapshot()})

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
        bc = cards.get(req.path_params["key"])
        if action == "approve" and bc and (why := cards.over_cap(bc)):
            return JSONResponse({"error": why}, status_code=409)  # before ACT is touched
        if bc is not None and bc.get("build"):
            if builds is None:  # builds switched off after the card was raised: never start a card helper
                return JSONResponse({"error": "builds are not configured; the card cannot be decided"},
                                    status_code=503)
            err = await _decide_build(cards, builds, build_lock, bc, action)  # None = ACT updated (or already in that state)
            if err is not None:
                return err
        try:
            card = (cards.approve if action == "approve" else cards.decline)(req.path_params["key"], via)
        except CardError as e:
            return JSONResponse({"error": str(e)}, status_code=404 if str(e) == "not found" else 409)
        if card.get("build"):
            engine.on_card(card)
            return JSONResponse(_public(card))
        engine.on_card(card)
        if card["state"] == "approved":
            start_card_helper(engine, helpers, card, via)
        return JSONResponse(_public(card))

    async def cancel_helper(req: Request):
        return JSONResponse({"cancelled": helpers.cancel(req.path_params["id"])})

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
                             Route("/attachments", post_attachment, methods=["POST"]),
                             Route("/history", history), Route("/events", events),
                             Route("/status", status),
                             Route("/terminal", terminal, methods=["POST"]),
                             Route("/autopilot", autopilot, methods=["POST"]),
                             Route("/cancel", cancel, methods=["POST"])]
                     + ([Route("/cards", list_cards),
                         Route("/cards/{key}/{action:str}", decide, methods=["POST"])] if cards else [])
                     + ([Route("/helpers/{id}/cancel", cancel_helper, methods=["POST"])] if helpers else []),
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
        _refused = False

        def connection_made(self, transport):
            try:
                sock = transport.get_extra_info("socket")
                pid = _peer_pid(sock) if sock is not None else None
                refuse = pid is not None and is_descendant(pid, os.getpid())
            except Exception as exc:  # the check itself failed (e.g. /bin/ps could not be spawned):
                # fail closed, and never raise into asyncio's callback (2026-11-07 audit)
                self.logger.warning("conductor: refused a connection whose peer could not be checked: %r", exc)
                refuse = True
            if refuse:
                self._refused = True
                transport.close()
                return
            super().connection_made(transport)

        # a refused connection never ran uvicorn's setup (nor did one whose connection_made did not
        # complete): its own callbacks would raise AttributeError into the log every time
        # (2026-11-02 log noise), so they do nothing unless uvicorn's transport is in place
        def _live(self):
            return not self._refused and getattr(self, "transport", None) is not None

        def data_received(self, data):
            if self._live():
                super().data_received(data)

        def connection_lost(self, exc):
            if self._live():
                super().connection_lost(exc)

        def eof_received(self):
            if self._live():
                return super().eof_received()
    return Guarded


def make_builds(root: Path, on_finish=None):
    """The builds registry, only when NAVIGATOR_ACT_BASE_URL is set (unset = builds are off)."""
    base = os.getenv("NAVIGATOR_ACT_BASE_URL")
    if not base:
        print("conductor: NAVIGATOR_ACT_BASE_URL unset - builds are not configured", file=sys.stderr)
        return None
    try:
        from navigator.clients.act_client import HTTPACTClient
        from navigator.config import Settings
        from navigator.conductor.builds import Builds
        # Settings.from_env reads NAVIGATOR_REPOS_FILE like the daemon, so tier-1 uses the daemon's repo config
        settings = Settings.from_env()
        return Builds(Path(root) / "builds.json", HTTPACTClient(base), settings=settings,
                      on_finish=on_finish, est_cost_usd=settings.max_cost_usd)
    except Exception as e:  # the conductor must still start; builds are simply off
        print(f"conductor: builds disabled, setup failed ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def serve(socket_path: Path, root: Path, runner=None) -> None:
    """Run the conductor on `socket_path` (replacing a stale file) with state in `root`."""
    import socket
    import uvicorn
    from functools import partial
    from navigator.conductor import gate, telegram
    from navigator.conductor.cards import Cards
    from navigator.conductor.helpers import Helpers, sdk_helper
    from navigator.conductor.tools import make_conductor_server
    cards = Cards(Path(root) / "cards.json")
    creds = telegram.load_creds()
    engine_ref: list[Engine] = []
    hook = gate.make_hook(cards, lambda card: engine_ref[0].on_card(card))
    # helpers use the same card hook: a gated action in a helper raises a card exactly as before
    helpers = Helpers(Path(root) / "helpers.json", runner=partial(sdk_helper, hook=hook, pid_root=Path(root) / "helper-pids"),
                      on_event=lambda hid, ev: engine_ref[0].on_helper_event(hid, ev),
                      on_finish=lambda rec: engine_ref[0].on_helper_finish(rec))
    builds = make_builds(root)
    if builds is not None:
        builds.on_needs_approval = make_build_card_callback(cards, engine_ref)
    if runner is None:
        from navigator.conductor.runner import sdk_turn
        runner = partial(sdk_turn, hook=hook, conductor_server=make_conductor_server(helpers, builds))
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
                    cards_pending=lambda: bool(cards.pending()), helpers=helpers, builds=builds)
    engine_ref.append(engine)
    from navigator import credits
    credits.set_notifier(engine.notice)
    # bound here, not by uvicorn: uvicorn chmods its own unix sockets to 0666
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    uvicorn.Server(uvicorn.Config(build_app(engine, manage_engine=True, cards=cards, helpers=helpers,
                                                builds=builds, attachments_dir=attachments.ATTACH_DIR),
                                  log_level="warning", http=_guarded_protocol())).run(sockets=[sock])
