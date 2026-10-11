import re
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
    — which resolve to the worktree root — and "../x" / absolute paths are rejected
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

_FAILED_TESTS = re.compile(r"(\d+) failed")

def _requires_tests(wt: Path, changed: list[str]) -> bool:
    # a python change in a repo that has a test suite must be checked by that suite;
    # live 2026-10-10 a verifier "passed" a .py change in 48s with no tests run
    return any(c.endswith(".py") for c in changed) and (wt / "tests").is_dir()

def _runs_tests(commands: list[list[str]]) -> bool:
    # pytest as the program, or launched through uv (`uv run [--flags] pytest ...`)
    return any(c and (c[0] == "pytest" or (c[:2] == ["uv", "run"] and "pytest" in c))
               for c in commands)

def run_build_and_verify(driver: SubagentDriver, worktree: WorktreeHandle,
                         run_id: str, runner: CommandRunner,
                         proposal: dict | None = None,
                         breakers: CircuitBreakers | None = None,
                         started_monotonic: float | None = None,
                         workdir: str = ".") -> PhaseResult:
    res = PhaseResult()
    wt = Path(worktree.path)
    run_dir = _resolve_write_target(wt, workdir) if workdir not in (".", "") else wt
    proposal = proposal or {}
    started = time.monotonic() if started_monotonic is None else started_monotonic
    build_ctx = {"proposal": proposal,
                 "files": _read_affected(wt, proposal.get("affected_files", []))}
    build = driver.run_role(DriverRequest(role=Role.builder, context=build_ctx, worktree_path=str(wt)))
    _check_scope([rel for rel, _ in build.file_writes], proposal.get("affected_files", []))
    if breakers:
        breakers.check_duration(started)
    for n, (relpath, content) in enumerate(build.file_writes, start=1):
        target = _resolve_write_target(wt, relpath)
        cp_before = checkpoint(wt, run_id=run_id, n=n, label=f"pre-write-{relpath}")
        res.checkpoints.append(cp_before)
        target.write_text(content)
        res.file_writes.append(relpath)
    checkpoint(wt, run_id=run_id, n=len(build.file_writes) + 1, label="post-build")
    # verifier — a verify phase with no commands is a failure, not a silent pass
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
    return res
