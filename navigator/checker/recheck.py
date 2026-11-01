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
        return 126, f"checker re-check timed out after {timeout_s}s"
    return p.returncode, p.stdout + p.stderr
