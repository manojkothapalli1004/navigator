import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from navigator.safety.circuit_breakers import CircuitBreakers
from navigator.safety.errors import CommandRejected
from navigator.runtime.drivers.base import DriverRequest, SubagentDriver
from navigator.runtime.worktree import WorktreeHandle
from navigator.runtime.checkpoint import checkpoint
from navigator.safety.command_runner import CommandRunner
from navigator.contracts.enums import Role

@dataclass
class PhaseResult:
    file_writes: list[str] = field(default_factory=list)
    checkpoints: list[str] = field(default_factory=list)
    verify_returncode: int = 0
    verify_output: str = ""
    verify_log: list[dict] = field(default_factory=list)

def _resolve_write_target(wt: Path, relpath: str) -> Path:
    """Resolve a builder write target, rejecting escapes and the worktree root itself.

    A valid target must be a path strictly inside the worktree (so relpath="." / ""
    \u2014 which resolve to the worktree root \u2014 and "../x" / absolute paths are rejected
    with a clean ValueError rather than a later IsADirectoryError or an escape).
    """
    wt = Path(wt).resolve()
    target = (wt / relpath).resolve()
    if target == wt or wt not in target.parents:
        raise ValueError(f"write outside worktree (or is the worktree root): {target}")
    return target

_MAX_FILE_CHARS = 60_000

def _read_affected(wt: Path, relpaths: list[str]) -> dict[str, str | None]:
    # tools are disabled for subagents, so the builder only sees what we hand it;
    # None marks a file the proposal expects to create
    files: dict[str, str | None] = {}
    for rel in relpaths:
        try:
            target = _resolve_write_target(wt, rel)
        except ValueError:
            continue
        files[rel] = target.read_text()[:_MAX_FILE_CHARS] if target.is_file() else None
    return files

def _is_test_file(rel: str) -> bool:
    # tests may be added beyond the approved list: they can't change runtime behaviour
    p = Path(rel)
    return p.suffix == ".py" and ("tests" in p.parts[:-1] or p.name.startswith("test_"))

def _check_scope(relpaths: list[str], allowed: list[str]) -> None:
    # the operator approved a proposal naming these files; the builder may not widen it
    if not allowed:
        return
    approved = {Path(a).as_posix() for a in allowed}
    for rel in relpaths:
        if Path(rel).as_posix() not in approved and not _is_test_file(rel):
            raise ValueError(f"builder wrote {rel!r} outside the approved affected_files")

# live 2026-10-12: a run added 11 tests that skip when a guessed API is missing; the
# suite passed while verifying nothing. xfail is deliberately not counted.
_SKIP_MARKERS = ("pytest.skip(", "pytest.importorskip(", "@pytest.mark.skip", "skipif")

def _count_skips(text: str) -> int:
    return sum(text.count(m) for m in _SKIP_MARKERS)

def _added_skips(wt: Path, writes: list[tuple[str, str]]) -> list[str]:
    """'<file>: <n>' for each written test file whose skip count grew versus the
    file on disk before the write (empty for new files). Must run before writing."""
    out = []
    for rel, content in writes:
        if not _is_test_file(rel):
            continue
        target = _resolve_write_target(wt, rel)
        original = target.read_text(errors="replace") if target.is_file() else ""
        n = _count_skips(content) - _count_skips(original)
        if n > 0:
            out.append(f"{rel}: {n}")
    return out

_FAILED_TESTS = re.compile(r"(\d+) failed")

def _requires_tests(wt: Path, changed: list[str]) -> bool:
    # a python change in a repo that has a test suite must be checked by that suite;
    # live 2026-10-10 a verifier "passed" a .py change in 48s with no tests run
    return any(c.endswith(".py") for c in changed) and (wt / "tests").is_dir()

def _runs_tests(commands: list[list[str]]) -> bool:
    # pytest as the program, or launched through uv (`uv run [--flags] pytest ...`)
    return any(c and (c[0] == "pytest" or (c[:2] == ["uv", "run"] and "pytest" in c))
               for c in commands)

_FAILED_FILE = re.compile(r"FAILED\s+(\S+?\.py)::")

def _failing_test_files(failure: str, wt: Path, workdir: str) -> list[str]:
    """Test files named in a previous attempt's pytest failures. They go to the
    builder in full so a retry can update them (live 2026-10-11)."""
    out = []
    for rel in dict.fromkeys(_FAILED_FILE.findall(failure or "")):
        for cand in (f"{workdir.rstrip('/')}/{rel}" if workdir not in (".", "") else rel, rel):
            if (wt / cand).is_file() and _is_test_file(cand):
                out.append(cand)
                break
    return out

_RELATED_BUDGET = 60_000
_NAMED_BUDGET = 60_000
_TRUNCATED_MARKER = "\n# [truncated by navigator]"

def _mention_position(rel: str, text: str, workdir: str) -> int | None:
    """Index of the first literal mention of `rel` (repo-relative, or relative to
    workdir) in `text`, bounded so 'x.py' does not match inside 'services/x.py'."""
    if not text:
        return None
    cands = [rel]
    if workdir not in (".", ""):
        prefix = workdir.rstrip("/") + "/"
        if rel.startswith(prefix) and len(rel) > len(prefix):
            cands.append(rel[len(prefix):])
    best = None
    for c in cands:
        m = re.search(r"(?<![\w./-])" + re.escape(c) + r"(?![\w/])", text)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best

def _related_files(wt: Path, tracked: list[str], skip: set[str],
                   mention_text: str = "", workdir: str = ".") -> dict[str, str]:
    """Read-only contents of other python files. Builders that only saw the files
    they edit wrote code against guessed models and fixtures (live 2026-10-11).

    Two separate allowances:
    (A) files the proposal names literally in mention_text, in order of first
        mention, up to _NAMED_BUDGET chars; the file that overflows is truncated
        (with a marker) and no further named files are added (live 2026-10-12:
        test-only proposals never showed the builder the modules they named);
    (B) the remaining python files, smallest first, whole files only, up to
        _RELATED_BUDGET.
    """
    candidates = []
    for rel in tracked:
        if rel.endswith(".py") and rel not in skip:
            p = wt / rel
            if p.is_file():
                candidates.append(rel)
    out: dict[str, str] = {}
    # (A) named allowance
    named = []
    for rel in candidates:
        pos = _mention_position(rel, mention_text or "", workdir or ".")
        if pos is not None:
            named.append((pos, rel))
    remaining = _NAMED_BUDGET
    for _, rel in sorted(named):
        if remaining <= 0:
            break
        text = (wt / rel).read_text(errors="replace")
        if len(text) <= remaining:
            out[rel] = text
            remaining -= len(text)
        else:
            out[rel] = text[:remaining] + _TRUNCATED_MARKER
            break
    # (B) general allowance, separate budget
    sized = []
    for rel in candidates:
        if rel not in out:
            sized.append(((wt / rel).stat().st_size, rel))
    used = 0
    for size, rel in sorted(sized):
        if used + size > _RELATED_BUDGET:
            break
        out[rel] = (wt / rel).read_text(errors="replace")
        used += size
    return out

def _normalize_affected(wt: Path, paths: list[str], workdir: str) -> list[str]:
    """Map workdir-relative paths onto repo-relative ones. Planners sometimes name
    files relative to the repo's workdir (e.g. "app/x.py" for backend/app/x.py)."""
    if workdir in (".", ""):
        return list(paths)
    out = []
    for p in paths:
        if p.startswith(workdir.rstrip("/") + "/") or (wt / p).exists():
            out.append(p)
        elif (wt / workdir / p).exists() or _is_test_file(p) or (wt / workdir / Path(p).parts[0]).is_dir():
            out.append(f"{workdir.rstrip('/')}/{p}")
        else:
            out.append(p)
    return out

def run_build_and_verify(driver: SubagentDriver, worktree: WorktreeHandle,
                         run_id: str, runner: CommandRunner,
                         proposal: dict | None = None,
                         breakers: CircuitBreakers | None = None,
                         started_monotonic: float | None = None,
                         workdir: str = ".") -> PhaseResult:
    res = PhaseResult()
    wt = Path(worktree.path)
    run_dir = _resolve_write_target(wt, workdir) if workdir not in (".", "") else wt
    proposal = dict(proposal or {})
    proposal["affected_files"] = _normalize_affected(wt, proposal.get("affected_files", []), workdir)
    started = time.monotonic() if started_monotonic is None else started_monotonic
    tracked = subprocess.run(["git", "ls-files"], cwd=wt, capture_output=True, text=True).stdout.split()
    failing = _failing_test_files(proposal.get("previous_attempt_failure", ""), wt, workdir)
    files = _read_affected(wt, list(dict.fromkeys(proposal.get("affected_files", []) + failing)))
    mention_text = f"{proposal.get('intended_changes') or ''} {proposal.get('rationale') or ''}"
    build_ctx = {"proposal": proposal, "files": files,
                 "test_files": [f for f in tracked if _is_test_file(f)][:200],
                 "related_files": _related_files(wt, tracked, set(files),
                                                 mention_text=mention_text,
                                                 workdir=workdir or ".")}
    build = driver.run_role(DriverRequest(role=Role.builder, context=build_ctx, worktree_path=str(wt)))
    _check_scope([rel for rel, _ in build.file_writes], proposal.get("affected_files", []))
    if breakers:
        breakers.check_duration(started)
    # compare against the originals before they are overwritten
    added_skips = _added_skips(wt, build.file_writes)
    for n, (relpath, content) in enumerate(build.file_writes, start=1):
        target = _resolve_write_target(wt, relpath)
        cp_before = checkpoint(wt, run_id=run_id, n=n, label=f"pre-write-{relpath}")
        res.checkpoints.append(cp_before)
        target.write_text(content)
        res.file_writes.append(relpath)
    checkpoint(wt, run_id=run_id, n=len(build.file_writes) + 1, label="post-build")
    if added_skips:
        # a skipped test verifies nothing; don't let the suite "pass" on them
        res.verify_returncode = 1
        res.verify_output = "builder added test skips: " + "; ".join(added_skips)
        return res
    # verifier \u2014 a verify phase with no commands is a failure, not a silent pass
    # commands run in the workdir, so hand the verifier paths relative to it
    rel_changed = [str(Path(f).relative_to(workdir)) if workdir not in (".", "")
                   and Path(f).is_relative_to(workdir) else f for f in res.file_writes]
    verify_ctx = {"proposal": proposal, "changed_files": rel_changed,
                  "allowed_commands": list(runner.allowlist),
                  "workdir": workdir or "."}
    verify = driver.run_role(DriverRequest(role=Role.verifier, context=verify_ctx, worktree_path=str(wt)))
    if not verify.commands:
        res.verify_returncode = 1
        res.verify_output = "verifier produced no commands"
        return res
    code_changed = [f for f in res.file_writes if f.endswith(".py") and not _is_test_file(f)]
    if (_requires_tests(run_dir, res.file_writes) and code_changed
            and not any(_is_test_file(f) for f in res.file_writes)):
        res.verify_returncode = 1
        res.verify_output = ("builder changed code but made no test changes: "
                             + ", ".join(code_changed))
        return res
    if _requires_tests(run_dir, res.file_writes) and not _runs_tests(verify.commands):
        res.verify_returncode = 1
        res.verify_output = ("verifier did not run the test suite for a python change: "
                             + "; ".join(" ".join(c) for c in verify.commands))
        return res
    for argv in verify.commands:
        if breakers:
            breakers.check_duration(started)
        try:
            r = runner.run(argv, cwd=run_dir)
        except CommandRejected as e:
            # a disallowed command is a verification result, not a crash
            res.verify_returncode = 126
            res.verify_output = f"verifier command rejected: {e}"
            res.verify_log.append({"argv": " ".join(argv), "returncode": 126,
                                   "output_tail": res.verify_output[-1500:]})
            if breakers:
                breakers.record_failure(f"rejected:{argv[0]}")
            break
        res.verify_returncode = r.returncode
        res.verify_output = r.stdout + r.stderr
        res.verify_log.append({"argv": " ".join(argv), "returncode": r.returncode,
                               "output_tail": res.verify_output[-1500:]})
        if r.returncode != 0:
            if breakers:
                m = _FAILED_TESTS.search(res.verify_output)
                if m:
                    breakers.check_failed_tests(int(m.group(1)))
                breakers.record_failure(" ".join(argv))
            break
    if res.verify_returncode == 0 and _requires_tests(run_dir, res.file_writes):
        _run_full_suite(runner, run_dir, res, breakers)
    return res


def _run_full_suite(runner, run_dir: Path, res: PhaseResult, breakers) -> None:
    # the verifier picks its own commands and may skip suites its change breaks
    # (live 2026-10-11); Navigator always runs the repo's whole suite itself
    entry = next((a for a in runner.allowlist if "pytest" in a.split()), None)
    if entry is None:
        res.verify_returncode = 1
        res.verify_output = "repo has tests but no pytest command is allowlisted"
        return
    argv = entry.split() + ["-q"]
    try:
        r = runner.run(argv, cwd=run_dir)
        rc, out = r.returncode, r.stdout + r.stderr
    except CommandRejected as e:
        rc, out = 126, f"full-suite command rejected: {e}"
    res.verify_log.append({"argv": " ".join(argv), "returncode": rc, "output_tail": out[-1500:]})
    res.verify_returncode, res.verify_output = rc, out
    if rc != 0 and breakers:
        m = _FAILED_TESTS.search(out)
        if m:
            breakers.check_failed_tests(int(m.group(1)))
