import subprocess
from dataclasses import dataclass
from pathlib import Path
from navigator.safety.errors import CommandRejected

_METACHARS = set(";|&$`><\n*?!")
# Flags that let an otherwise-allowlisted command escape the worktree boundary
# (e.g. `git diff --no-index <abs> <abs>` reads arbitrary files regardless of cwd).
_REJECTED_FLAGS = {"--no-index"}

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

class CommandRunner:
    def __init__(self, allowlist: list[str], worktree_root: Path, timeout_s: float = 60.0):
        self.allowlist = list(allowlist)
        self.worktree_root = Path(worktree_root).resolve()
        self.timeout_s = timeout_s

    def _is_allowed(self, argv: list[str]) -> bool:
        joined = " ".join(argv)
        return any(joined == a or joined.startswith(a + " ") for a in self.allowlist)

    def run(self, argv: list[str], cwd: Path) -> CommandResult:
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise CommandRejected("argv must be a list[str]")
        for token in argv:
            if any(ch in _METACHARS for ch in token):
                raise CommandRejected(f"shell metacharacter in token: {token!r}")
            if token in _REJECTED_FLAGS:
                raise CommandRejected(f"rejected flag: {token!r}")
            if token.startswith("/"):
                raise CommandRejected(f"absolute path token not allowed: {token!r}")
        if not self._is_allowed(argv):
            raise CommandRejected(f"command not on allowlist: {' '.join(argv)}")
        cwd = Path(cwd).resolve()
        if cwd != self.worktree_root and self.worktree_root not in cwd.parents:
            raise CommandRejected(f"cwd outside worktree: {cwd}")
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  shell=False, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise CommandRejected(f"command timed out after {self.timeout_s}s: {' '.join(argv)}") from e
        except OSError as e:
            raise CommandRejected(f"command failed to start: {' '.join(argv)} ({e})") from e
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
