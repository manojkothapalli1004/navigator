"""Build a branch of the ACT console into a throwaway preview on :3001, and stop it again.

Only the process listening on the preview port is ever killed: a broad `pkill next-server` also
takes down the live console on :3000 (2026-10-31)."""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ACT_REPO = Path("/Users/Manoj/external-tools/agent-control-tower")
PREVIEW_PORT = 3001
LOCK_PATH = Path("/Users/Manoj/calwbot/logs/navigator/checker/preview.lock")


class PreviewBusy(RuntimeError):
    """Another verify holds the preview port."""


@contextmanager
def lock(path: Path = LOCK_PATH, timeout_s: float = 900):
    """Exclusive hold on the preview port for one build+check; waits up to timeout_s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PreviewBusy(f"preview busy (lock {path})")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def stop(port: int = PREVIEW_PORT) -> None:
    out = subprocess.run(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    for pid in (int(p) for p in out.split() if p.strip().isdigit()):
        os.kill(pid, signal.SIGTERM)


def build(source: str | Path, work: Path, port: int = PREVIEW_PORT) -> str:
    """Build a preview into `work` and start it on `port`; returns the base URL. `source` is a
    frontend directory (a run worktree's, copied as it is, uncommitted edits included) or a
    branch of the ACT repo (exported with git archive)."""
    stop(port)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    fe = work / "frontend"
    if Path(source).is_dir():
        shutil.copytree(source, fe, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".next"))
    else:
        # git archive reads the branch without touching the live repo's index or working tree
        tar = work / "frontend.tar"
        subprocess.run(["git", "archive", "--format=tar", f"--output={tar}", str(source), "frontend"],
                       cwd=ACT_REPO, check=True)
        subprocess.run(["tar", "-xf", str(tar), "-C", str(work)], check=True)
    subprocess.run(["cp", "-cR", str(ACT_REPO / "frontend" / "node_modules"), str(fe / "node_modules")], check=True)
    subprocess.run([str(fe / "node_modules/.bin/next"), "build"], cwd=fe, check=True, capture_output=True, text=True)
    return _start_and_wait(fe, port)


def _start_and_wait(fe: Path, port: int) -> str:
    subprocess.Popen([str(fe / "node_modules/.bin/next"), "start", "--port", str(port), "--hostname", "127.0.0.1"],
                     cwd=fe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    # no proxies: under the sealed verifier env HTTP(S)_PROXY points at a dead port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            opener.open(base + "/", timeout=2)
            return base
        except Exception:
            time.sleep(1)
    stop(port)
    raise RuntimeError(f"preview did not start on {base}")
