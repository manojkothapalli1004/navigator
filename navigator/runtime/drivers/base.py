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

@dataclass
class DriverResult:
    role: Role
    proposals: list[dict] = field(default_factory=list)   # planner output
    file_writes: list[tuple[str, str]] = field(default_factory=list)  # (relpath, content) builder
    commands: list[list[str]] = field(default_factory=list)  # builder/verifier argv to run
    summary: str = ""

class SubagentDriver(Protocol):
    def run_role(self, request: DriverRequest) -> DriverResult: ...
