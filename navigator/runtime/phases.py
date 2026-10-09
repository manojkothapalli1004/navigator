from dataclasses import dataclass, field
from pathlib import Path
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

def run_build_and_verify(driver: SubagentDriver, worktree: WorktreeHandle,
                         run_id: str, runner: CommandRunner,
                         proposal: dict | None = None) -> PhaseResult:
    res = PhaseResult()
    wt = Path(worktree.path)
    proposal = proposal or {}
    build_ctx = {"proposal": proposal,
                 "files": _read_affected(wt, proposal.get("affected_files", []))}
    build = driver.run_role(DriverRequest(role=Role.builder, context=build_ctx, worktree_path=str(wt)))
    for n, (relpath, content) in enumerate(build.file_writes, start=1):
        target = _resolve_write_target(wt, relpath)
        cp_before = checkpoint(wt, run_id=run_id, n=n, label=f"pre-write-{relpath}")
        res.checkpoints.append(cp_before)
        target.write_text(content)
        res.file_writes.append(relpath)
    checkpoint(wt, run_id=run_id, n=len(build.file_writes) + 1, label="post-build")
    # verifier — a verify phase with no commands is a failure, not a silent pass
    verify_ctx = {"proposal": proposal, "changed_files": list(res.file_writes),
                  "allowed_commands": list(runner.allowlist)}
    verify = driver.run_role(DriverRequest(role=Role.verifier, context=verify_ctx, worktree_path=str(wt)))
    if not verify.commands:
        res.verify_returncode = 1
        res.verify_output = "verifier produced no commands"
        return res
    for argv in verify.commands:
        r = runner.run(argv, cwd=wt)
        res.verify_returncode = r.returncode
        res.verify_output = r.stdout + r.stderr
        if r.returncode != 0:
            break
    return res
