import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from navigator.safety.errors import CommandRejected

_METACHARS = set(";|&$`><\n*?!")
# Flags that let an otherwise-allowlisted command escape the worktree boundary
# (e.g. `git diff --no-index <abs> <abs>` reads arbitrary files regardless of cwd).
_REJECTED_FLAGS = {"--no-index"}
# Exact flag allowlist for LLM-issued git commands (trusted runtime git calls
# bypass the runner). Any other dashed token in a git argv is rejected.
# status: only the exact value-bearing spellings --untracked-files=no / -uno (hides
# untracked files; live 2026-10: verifiers copy executor.py). The two-token form
# `--untracked-files no`, bare `-u`, and any other value (=all, =normal, -uall)
# stay rejected.
_GIT_SAFE_FLAGS = {
    "status": {"--porcelain", "-s", "--short", "-b", "--", "--untracked-files=no", "-uno"},
    "diff": {"--stat", "--name-only", "--name-status", "--cached", "--staged", "--"},
    "log": {"--oneline", "--name-only", "--stat", "-1", "-5", "-10", "-20", "--"},
}
# Count flags accepted per subcommand: `-n <int>`, `-n<int>`, `--max-count <int>`,
# `--max-count=<int>` with 1 <= int <= _GIT_MAX_COUNT. The integer is consumed as
# the flag's value (live: verifiers naturally write `git log -n 5`).
_GIT_COUNT_FLAGS = {"log": ("-n", "--max-count")}
_GIT_MAX_COUNT = 50
# Longest digit string a valid count can have (_GIT_MAX_COUNT has 2 digits). Checked
# before int() so huge inputs cannot hit Python's int-string-conversion limit.
_GIT_MAX_COUNT_DIGITS = len(str(_GIT_MAX_COUNT))


def _allowed_flags_display(subcommand: str) -> list[str]:
    flags = sorted(_GIT_SAFE_FLAGS.get(subcommand, set()))
    for f in _GIT_COUNT_FLAGS.get(subcommand, ()):
        flags.append(f"{f} <1-{_GIT_MAX_COUNT}>")
    return flags


def git_flag_allowlist_text() -> str:
    """Human/LLM-readable per-subcommand git flag allowlist (prompt material)."""
    parts = []
    for sub in sorted(_GIT_SAFE_FLAGS):
        line = f"git {sub}: {', '.join(_allowed_flags_display(sub))}"
        if "--max-count" in _GIT_COUNT_FLAGS.get(sub, ()):
            line += f" (also -n<N> and --max-count=<N>, N in 1-{_GIT_MAX_COUNT})"
        parts.append(line)
    return "; ".join(parts)


def _count_ok(raw: str | None) -> bool:
    """True iff raw is an ASCII decimal integer in 1.._GIT_MAX_COUNT. Never raises."""
    if raw is None or not isinstance(raw, str):
        return False
    if not raw or len(raw) > _GIT_MAX_COUNT_DIGITS:
        return False
    if not raw.isascii() or not raw.isdigit():
        return False
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return False
    return 1 <= value <= _GIT_MAX_COUNT


def _split_count(token: str, args: list[str], i: int, count_flags: tuple):
    """Return (flag, value, tokens_consumed) if token is a count flag, else None."""
    if token in count_flags:
        value = args[i + 1] if i + 1 < len(args) else None
        return token, value, 2
    if "--max-count" in count_flags and token.startswith("--max-count="):
        return "--max-count", token.split("=", 1)[1], 1
    if "-n" in count_flags and token.startswith("-n") and not token.startswith("--") and len(token) > 2:
        return "-n", token[2:], 1
    return None


def _check_git_flags(subcommand: str, args: list[str]) -> None:
    safe = _GIT_SAFE_FLAGS.get(subcommand, set())
    count_flags = _GIT_COUNT_FLAGS.get(subcommand, ())
    flags = _allowed_flags_display(subcommand)
    allowed = ", ".join(flags) if flags else "none"
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("-") and token not in safe:
            split = _split_count(token, args, i, count_flags) if count_flags else None
            if split is not None:
                flag, value, consumed = split
                if not _count_ok(value):
                    shown = value if value is None or len(value) <= 40 else value[:40] + "..."
                    raise CommandRejected(
                        f"git flag not on safe list for 'git {subcommand}': {token[:60]!r} "
                        f"needs an integer count 1-{_GIT_MAX_COUNT} for {flag} (got {shown!r}) "
                        f"(allowed: {allowed})"
                    )
                i += consumed
                continue
            raise CommandRejected(
                f"git flag not on safe list for 'git {subcommand}': {token!r} "
                f"(allowed: {allowed})"
            )
        i += 1


def _clean_env() -> dict[str, str]:
    # commands run the target repo's tooling; they must not see Navigator's own
    # configuration (live 2026-10-10: NAVIGATOR_DRIVER leaked into a verifier pytest)
    return {k: v for k, v in os.environ.items() if not k.startswith("NAVIGATOR_")}

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
        # git global options before the subcommand (-c, --git-dir, --exec-path, ...)
        # can run arbitrary programs or escape the worktree; never allow them, even if
        # an operator allowlists bare 'git'.
        if argv and argv[0] == "git" and len(argv) >= 2 and argv[1].startswith("-"):
            raise CommandRejected(f"git global option before subcommand not allowed: {argv[1]!r}")
        if not self._is_allowed(argv):
            raise CommandRejected(f"command not on allowlist: {' '.join(argv)}")
        if argv[0] == "git" and len(argv) >= 2:
            _check_git_flags(argv[1], argv[2:])
        cwd = Path(cwd).resolve()
        if cwd != self.worktree_root and self.worktree_root not in cwd.parents:
            raise CommandRejected(f"cwd outside worktree: {cwd}")
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  shell=False, timeout=self.timeout_s, env=_clean_env())
        except subprocess.TimeoutExpired as e:
            raise CommandRejected(f"command timed out after {self.timeout_s}s: {' '.join(argv)}") from e
        except OSError as e:
            raise CommandRejected(f"command failed to start: {' '.join(argv)} ({e})") from e
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
