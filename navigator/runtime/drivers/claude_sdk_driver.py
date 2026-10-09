import json
from typing import Callable
from navigator.runtime.drivers.base import ROLE_PERMISSIONS, DriverRequest, DriverResult
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.errors import DriverParseError

_PROPOSAL_KEYS = {"title": str, "rationale": str, "intended_changes": str, "affected_files": list}

_ROLE_OUTPUT_SPEC = {
    Role.planner: ('Respond with ONLY a JSON object: {"proposals": [{"title": "...", '
                   '"rationale": "...", "intended_changes": "...", "affected_files": ["path", ...]}], '
                   '"summary": "..."}'),
    Role.builder: ('Implement context.proposal by editing context.files (null = new file). '
                   'Respond with ONLY a JSON object: {"file_writes": [[relpath, FULL new file content], ...], '
                   '"commands": [], "summary": "..."}. Each content replaces the whole file — '
                   'never elide unchanged code. Only write paths listed in context.files.'),
    Role.verifier: ('Choose commands that prove context.proposal works, each starting with an entry '
                    'of context.allowed_commands. Respond with ONLY a JSON object: '
                    '{"commands": [[argv...], ...], "summary": "..."}'),
}

class ClaudeSDKDriver:
    """SubagentDriver backed by an injectable LLM query function.

    query_fn is (prompt: str) -> str so tests run offline; the real
    claude-agent-sdk binding is supplied at wiring time (sdk extra), never
    imported here.
    """
    def __init__(self, *, query_fn: Callable[[str], str], budget_guard: BudgetGuard,
                 estimated_cost_per_call_usd: float = 0.25):
        self.query_fn = query_fn
        self.budget_guard = budget_guard
        self.estimated_cost_per_call_usd = estimated_cost_per_call_usd

    def run_role(self, request: DriverRequest) -> DriverResult:
        self.budget_guard.check_before_call(self.estimated_cost_per_call_usd)
        raw = self.query_fn(self._build_prompt(request))
        self.budget_guard.record_spend(self.estimated_cost_per_call_usd)
        return self._parse(request.role, raw)

    def _build_prompt(self, request: DriverRequest) -> str:
        lines = [f"role: {request.role.value}", f"context: {json.dumps(request.context)}"]
        if request.worktree_path is not None:
            lines.append(f"worktree: {request.worktree_path}")
        lines.append(_ROLE_OUTPUT_SPEC[request.role])
        return "\n".join(lines)

    def _parse(self, role: Role, raw: str) -> DriverResult:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise DriverParseError(
                f"response is not valid JSON (len={len(raw)}): "
                f"head={raw[:120]!r} tail={raw[-120:]!r}") from e
        if not isinstance(payload, dict):
            raise DriverParseError(f"response must be a JSON object, got {type(payload).__name__}")

        perms = ROLE_PERMISSIONS[role]
        if payload.get("file_writes") and "write" not in perms:
            raise DriverParseError(f"{role.value} is not permitted to write files")
        if payload.get("commands") and "run" not in perms:
            raise DriverParseError(f"{role.value} is not permitted to run commands")

        summary = payload.get("summary", "")
        if not isinstance(summary, str):
            raise DriverParseError("summary must be a string")

        proposals: list[dict] = []
        if role == Role.planner:
            proposals = payload.get("proposals")
            if not isinstance(proposals, list) or not all(isinstance(p, dict) for p in proposals):
                raise DriverParseError("planner response must contain a 'proposals' list of objects")
            for p in proposals:
                for key, typ in _PROPOSAL_KEYS.items():
                    if not isinstance(p.get(key), typ):
                        raise DriverParseError(
                            f"proposal missing or mistyped {key!r} (expected {typ.__name__}): {p!r}"[:200])
                if not all(isinstance(f, str) for f in p["affected_files"]):
                    raise DriverParseError(f"affected_files must be a list of strings: {p['affected_files']!r}")

        file_writes = [self._parse_file_write(entry) for entry in payload.get("file_writes", [])]
        commands = [self._parse_command(entry) for entry in payload.get("commands", [])]
        return DriverResult(role=role, proposals=proposals,
                            file_writes=file_writes, commands=commands, summary=summary)

    @staticmethod
    def _parse_file_write(entry) -> tuple[str, str]:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or not all(isinstance(x, str) for x in entry)):
            raise DriverParseError(f"file_writes entries must be [relpath, content] string pairs, got {entry!r}")
        return (entry[0], entry[1])

    @staticmethod
    def _parse_command(entry) -> list[str]:
        if not isinstance(entry, list) or not entry or not all(isinstance(x, str) for x in entry):
            raise DriverParseError(f"commands entries must be non-empty argv string lists, got {entry!r}")
        return list(entry)
