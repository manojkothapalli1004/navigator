from dataclasses import dataclass, field
from typing import Protocol
from navigator.contracts.enums import Role

ROLE_PERMISSIONS: dict[Role, set[str]] = {
    Role.planner: {"read"},
    Role.builder: {"read", "write", "run"},
    Role.verifier: {"read", "run"},
}

@dataclass
class DriverRequest:
    role: Role
    context: dict
    worktree_path: str | None = None
    # build 2 (builder tests): the per-repo CommandRunner, the run directory inside the
    # worktree and the repo's allowlist, so the tooled builder's run_tests tool can run the
    # allowlisted pytest entry through phases._run_sealed; only the builder request sets them
    runner: object | None = None
    run_dir: str | None = None
    allowed_commands: tuple[str, ...] = ()
    # build 2b: the per-BUILD run_tests counter ({"used": int}) owned by phases.run_build_and_verify
    # and shared by the first attempt and every repair turn; None = a fresh per-call budget
    test_budget: dict | None = None

@dataclass
class DriverResult:
    role: Role
    proposals: list[dict] = field(default_factory=list)   # planner output
    file_writes: list[tuple[str, str]] = field(default_factory=list)  # (relpath, content) builder
    commands: list[list[str]] = field(default_factory=list)  # builder/verifier argv to run
    summary: str = ""

class SubagentDriver(Protocol):
    def run_role(self, request: DriverRequest) -> DriverResult: ...
