"""Don't check a console that hasn't been rebuilt yet (2026-11-02).

The scheduled run fires when ACT's main branch moves — right after a merge, before the operator
rebuilds the live frontend — and used to report the old build (or a restarting server) as faults
(42 false ones on 11-01). Before a LIVE run we wait until the build is newer than the last
frontend change on main and the console answers; past the timeout the run is skipped, not
reported. Backend-only merges never wait."""
from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

ACT_REPO = Path("/Users/Manoj/external-tools/agent-control-tower")
LIVE = "http://127.0.0.1:3000"
TIMEOUT_S = 900
POLL_S = 20


def frontend_changed_since_build(repo: Path = ACT_REPO) -> bool:
    """True when main has a frontend change newer than the live build (or there is no build).
    --first-parent: a merge commit counts with its own date, even if its branch commits are older."""
    try:
        built = (Path(repo) / "frontend" / ".next" / "BUILD_ID").stat().st_mtime
    except OSError:
        return True
    # newest first: the latest frontend-touching commits on main's first-parent line, as timestamps
    out = subprocess.run(["git", "log", "--first-parent", "--max-count=20", "--format=%ct", "main",
                          "--", "frontend"], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        return False  # can't tell: don't block the check on a git hiccup
    return any(int(t) > built for t in out.stdout.split() if t.isdigit())


def console_up(base: str = LIVE) -> bool:
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def wait_for_live_build(*, stale_fn=frontend_changed_since_build, up_fn=console_up, sleep=time.sleep,
                        clock=time.monotonic, timeout_s: float = TIMEOUT_S, poll_s: float = POLL_S) -> str | None:
    """None when the live console is rebuilt and answering; otherwise the reason after timeout_s."""
    start = clock()
    while True:
        stale = stale_fn()
        up = not stale and up_fn()
        if not stale and up:
            return None
        if clock() - start >= timeout_s:
            mins = round(timeout_s / 60)
            if stale:
                return f"the live console hasn't been rebuilt since the last frontend merge (waited {mins} min)"
            return f"the live console didn't answer for {mins} min"
        sleep(poll_s)
