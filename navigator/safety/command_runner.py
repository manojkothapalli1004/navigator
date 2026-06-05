import subprocess
from dataclasses import dataclass
from pathlib import Path
from navigator.safety.errors import CommandRejected

_METACHARS = set(";|&$`><\n*?!")

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

class CommandRunner:
    def __init__(self, allowlist: list[str], worktree_root: Path):
        self.allowlist = list(allowlist)
        self.worktree_root = Path(worktree_root).resolve()

    def _is_allowed(self, argv: list[str]) -> bool:
        joined = " ".join(argv)
        return any(joined == a or joined.startswith(a + " ") for a in self.allowlist)

    def run(self, argv: list[str], cwd: Path) -> CommandResult:
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise CommandRejected("argv must be a list[str]")
        for token in argv:
            if any(ch in _METACHARS for ch in token):
                raise CommandRejected(f"shell metacharacter in token: {token!r}")
        if not self._is_allowed(argv):
            raise CommandRejected(f"command not on allowlist: {' '.join(argv)}")
        cwd = Path(cwd).resolve()
        if cwd != self.worktree_root and self.worktree_root not in cwd.parents:
            raise CommandRejected(f"cwd outside worktree: {cwd}")
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, shell=False)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
