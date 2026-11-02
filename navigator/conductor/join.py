"""C5: the operator's terminal joins the one shared conversation.

Takes the conductor's turn lock (waiting out a running turn), tells the conductor the operator
is in the terminal, runs `claude --resume <shared session>` in the workspace with the operator's
own terminal, then shows the terminal's exchanges in the web/Telegram history and lets go."""
from __future__ import annotations

import fcntl
import subprocess
import sys
from pathlib import Path
from typing import Callable

from navigator.conductor import transcript
from navigator.conductor.runner import CWD
from navigator.conductor.store import Store

ROOT = Path("/Users/Manoj/calwbot/logs/conductor")
SOCKET = ROOT / "conductor.sock"

NO_SESSION = "No shared conversation yet — send one message from the website or Telegram first."
WAITING = "The chat service is answering — waiting for it to finish (Ctrl-C to cancel)…"
DOWN = "Chat service not running — this terminal session won't show in the web history."


MAX_ENTRIES, MAX_TEXT = 500, 20000  # the conductor's /terminal limits


def _clip(text: str) -> str:
    # one over-long reply must not make the conductor reject the whole import
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def socket_poster(socket: Path = SOCKET) -> Callable[[dict], dict]:
    import httpx

    def post(body: dict) -> dict:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)),
                          base_url="http://conductor", timeout=15) as c:
            r = c.post("/terminal", json=body)
            r.raise_for_status()
            return r.json()
    return post


def run_claude(argv: list[str], cwd: str) -> int:
    """Run the terminal's claude in the foreground. Ctrl-C belongs to claude (it interrupts a
    reply there), so this process ignores SIGINT while waiting; a SIGTERM/SIGHUP to this process
    stops claude before the turn lock is released, so two writers never share the session."""
    import signal

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    p = subprocess.Popen(argv, cwd=cwd)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGHUP, stop)
        return p.wait()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        if p.poll() is None:
            p.kill()
            p.wait()


def join(root: Path = ROOT, socket: Path = SOCKET, run=run_claude, post=None, out=sys.stdout,
         home: Path | None = None) -> int:
    import httpx
    if not Store(root).session_id:
        print(NO_SESSION, file=out)
        return 1
    post = post or socket_poster(socket)
    with open(Path(root) / "turn.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(WAITING, file=out, flush=True)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
            except KeyboardInterrupt:
                return 130
        try:
            # read under the lock: a turn we waited out may have had to start a fresh session
            sid = Store(root).session_id
            if not sid:
                print(NO_SESSION, file=out)
                return 1
            try:
                post({"state": "joined"})
                reachable = True
            except (httpx.HTTPError, OSError):
                print(DOWN, file=out, flush=True)
                reachable = False
            path = transcript.transcript_path(sid, CWD, home)
            offset = transcript.size(path)
            try:
                return run(["claude", "--resume", sid], cwd=CWD)
            finally:
                if reachable:
                    try:
                        entries = [{**e, "text": _clip(e["text"])}
                                   for e in transcript.read_since(path, offset)[-MAX_ENTRIES:]]
                        post({"state": "left", "entries": entries})
                    except (httpx.HTTPError, OSError):
                        print(DOWN, file=out, flush=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
