"""Build a branch of the ACT console into a throwaway preview on :3001, and stop it again.

Only the process listening on the preview port is ever killed: a broad `pkill next-server` also
takes down the live console on :3000 (2026-10-31)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

ACT_REPO = Path("/Users/Manoj/external-tools/agent-control-tower")
PREVIEW_PORT = 3001


def stop(port: int = PREVIEW_PORT) -> None:
    out = subprocess.run(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    for pid in (int(p) for p in out.split() if p.strip().isdigit()):
        os.kill(pid, signal.SIGTERM)


def build(branch: str, work: Path, port: int = PREVIEW_PORT) -> str:
    """Export `branch`'s frontend into `work`, build it and start it on `port`; returns the base URL."""
    stop(port)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    subprocess.run(["git", f"--work-tree={work}", "checkout", branch, "--", "frontend"], cwd=ACT_REPO, check=True)
    subprocess.run(["git", "reset", "-q"], cwd=ACT_REPO, check=True)   # the checkout above also staged into the index
    fe = work / "frontend"
    subprocess.run(["cp", "-cR", str(ACT_REPO / "frontend" / "node_modules"), str(fe / "node_modules")], check=True)
    subprocess.run([str(fe / "node_modules/.bin/next"), "build"], cwd=fe, check=True, capture_output=True)
    subprocess.Popen([str(fe / "node_modules/.bin/next"), "start", "--port", str(port), "--hostname", "127.0.0.1"],
                     cwd=fe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/", timeout=2)
            return base
        except Exception:
            time.sleep(1)
    stop(port)
    raise RuntimeError(f"preview of {branch} did not start on {base}")
