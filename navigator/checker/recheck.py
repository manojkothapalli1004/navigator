"""Run the site checker's `verify` on a worktree's frontend, from the Navigator daemon.

The checker needs Playwright, which lives in its own venv (.venv-checker), so the daemon calls
it as a subprocess. Kept import-light: runtime/phases imports this module."""
from __future__ import annotations

import subprocess
from pathlib import Path

NAVIGATOR_REPO = Path(__file__).resolve().parents[2]
CHECKER_PYTHON = NAVIGATOR_REPO / ".venv-checker" / "bin" / "python"
RECHECK_TIMEOUT_S = 1200  # preview build (~2 min) + a wait on the preview lock + browsing


def run_recheck(source: Path, fingerprints: list[str], timeout_s: float = RECHECK_TIMEOUT_S) -> tuple[int, str]:
    """(returncode, output) of `checker verify --source <source> --fingerprint ...` in the sealed
    verifier env: 0 every fault gone, 1 a fault is still there, 2 checker error, 126 timeout."""
    from navigator.runtime.phases import sealed_verifier_env  # here: phases imports this module
    if not Path(CHECKER_PYTHON).exists():
        return 1, f"checker venv missing: {CHECKER_PYTHON} (run deploy/install_site_checker.sh)"
    argv = [str(CHECKER_PYTHON), "-m", "navigator.checker", "verify", "--source", str(source)]
    for fp in fingerprints:
        argv += ["--fingerprint", fp]
    try:
        p = subprocess.run(argv, cwd=NAVIGATOR_REPO, capture_output=True, text=True,
                           timeout=timeout_s, env=sealed_verifier_env())
    except subprocess.TimeoutExpired:
        return 126, f"checker re-check timed out after {timeout_s}s" + _stop_orphaned_preview()
    return p.returncode, p.stdout + p.stderr


def _stop_orphaned_preview() -> str:
    """The timed-out checker was killed, but the `next start` it launched keeps :3001 (2026-11-02).
    Stop it only when nobody holds the preview lock: if another checker run holds it, its preview
    is in use and stays. Kills only the port's owner by PID (never a broad pkill; never :3000)."""
    import fcntl
    from navigator.checker import preview
    try:
        fh = open(preview.LOCK_PATH, "a")
    except OSError:
        return ""
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "; another checker run holds the preview, left it running"
        try:
            preview.stop(preview.PREVIEW_PORT)
        except Exception as e:
            return f"; could not stop the leftover preview ({type(e).__name__})"
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return f"; stopped the leftover preview on :{preview.PREVIEW_PORT}"
