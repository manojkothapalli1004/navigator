import json
import shlex
from pathlib import Path
from typing import Callable
from navigator.config import Settings
from navigator.runtime.drivers.base import ROLE_PERMISSIONS, DriverRequest, DriverResult
# N9b: phases._read_affected cuts context.files at this many chars with no marker; an
# edit resolved against such a base would silently drop the rest of the file
from navigator.runtime.phases import _MAX_FILE_CHARS
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import git_flag_allowlist_text
from navigator.safety.errors import DriverParseError

_PROPOSAL_KEYS = {"title": str, "rationale": str, "intended_changes": str, "affected_files": list}

_ROLE_OUTPUT_SPEC = {
    Role.planner: ('If context.snapshot.repo_brief is present it overrides general planning: follow '
                   'it exactly, using context.snapshot.context_files as evidence. '
                   'Respond with ONLY a JSON object: {"proposals": [{"title": "...", '
                   '"rationale": "...", "intended_changes": "...", "affected_files": ["path", ...]}], '
                   '"summary": "..."}'),
    Role.builder: ('Implement context.proposal by editing context.files (null = new file). '
                   'If context.proposal.previous_attempt_failure is present, a previous attempt '
                   'failed with that output: fix its cause (e.g. update existing tests that call '
                   'changed APIs). '
                   'Respond with ONLY a JSON object: {"file_edits": [[relpath, old, new], ...], '
                   '"file_writes": [[relpath, FULL new file content], ...], '
                   '"commands": [], "summary": "..."}. '
                   'Use file_edits for EXISTING files: each old is a snippet copied verbatim from '
                   'context.files (same indentation, whitespace and line breaks) with enough '
                   'surrounding lines to occur exactly once in that file, and new is its '
                   'replacement; an old that occurs zero or several times is rejected. Keep each '
                   'edit small; several edits to one file apply in order to the progressively '
                   'edited text. Use file_writes only for NEW files (null in context.files) or '
                   'when a full rewrite is genuinely shorter than the edits; a file_writes content '
                   'replaces the whole file \u2014 never elide unchanged code. Never list one path in '
                   'both file_edits and file_writes. '
                   'Only write paths listed in context.files, plus test files. '
                   'If the repo has tests, add or update tests that exercise the new or changed '
                   'behaviour (a passing suite that never runs the new path proves nothing). '
                   'You MAY add new test files not in context.files; follow the layout and '
                   'naming of context.test_files. context.related_files are for reference: use them '
                   'to get models, fixtures and APIs right. Never write non-test related files; '
                   'DO update existing test files among them when your change alters an API '
                   'they call.'),
    Role.verifier: ('Choose commands that prove context.proposal works, each starting with an entry '
                    'of context.allowed_commands. Commands run inside context.workdir; use paths '
                    'relative to it (context.changed_files already are). Respond with ONLY a JSON '
                    'object: {"commands": [[argv...], ...], "summary": "..."}'),
}

_COMMAND_ROLES = (Role.builder, Role.verifier)


def git_flags_prompt_line() -> str:
    return ("git flag allowlist per subcommand (applies to context.allowed_commands entries "
            "starting with git; any other dashed git flag is rejected): "
            + git_flag_allowlist_text())


def _shape(value) -> str:
    """Type-only description of a malformed response entry (never its text)."""
    if isinstance(value, (list, tuple)):
        kinds = ", ".join(type(x).__name__ for x in value)
        return f"{type(value).__name__} of {len(value)} ({kinds})"
    return type(value).__name__


class ClaudeSDKDriver:
    """SubagentDriver backed by an injectable LLM query function.

    query_fn is (prompt: str) -> str so tests run offline; the real
    claude-agent-sdk binding is supplied at wiring time (sdk extra), never
    imported here.
    """
    def __init__(self, *, query_fn: Callable[[str], str], budget_guard: BudgetGuard,
                 role_query_fns: dict[Role, Callable[[str], str]] | None = None,
                 estimated_cost_per_call_usd: float = 0.25):
        self.query_fn = query_fn
        self.role_query_fns = role_query_fns or {}
        self.budget_guard = budget_guard
        self.estimated_cost_per_call_usd = estimated_cost_per_call_usd

    @classmethod
    def from_settings(cls, settings: Settings, *, budget_guard: BudgetGuard,
                      make_query_fn: Callable[..., Callable[[str], str]] | None = None,
                      estimated_cost_per_call_usd: float = 0.25) -> "ClaudeSDKDriver":
        """Wire the live driver from Settings: per-role models (cheaper models where
        the role doesn't need the strongest one), the model fallback chain and ONE
        cooldown dict shared by every query fn, so a model that ran out of credits
        for the builder is skipped by the other roles too.

        make_query_fn is injectable (default: make_sdk_query_fn) so the wiring is
        testable without the sdk extra.
        """
        if make_query_fn is None:
            from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
            make_query_fn = make_sdk_query_fn
        cooldowns: dict = {}
        common = dict(timeout_s=settings.sdk_timeout_s,
                      fallbacks=tuple(settings.sdk_model_fallbacks),
                      cooldown_s=settings.sdk_model_cooldown_s,
                      cooldowns=cooldowns)
        role_fns = {Role(role): make_query_fn(model=model, **common)
                    for role, model in settings.sdk_role_models.items()}
        return cls(query_fn=make_query_fn(model=settings.sdk_model, **common),
                   role_query_fns=role_fns, budget_guard=budget_guard,
                   estimated_cost_per_call_usd=estimated_cost_per_call_usd)

    def run_role(self, request: DriverRequest) -> DriverResult:
        self.budget_guard.check_before_call(self.estimated_cost_per_call_usd)
        query_fn = self.role_query_fns.get(request.role, self.query_fn)
        raw = query_fn(self._build_prompt(request))
        self.budget_guard.record_spend(self.estimated_cost_per_call_usd)
        # N9: the builder's file_edits are resolved against the file contents it was
        # shown (context.files: relpath -> current text, None for a new file)
        files = None
        if request.role == Role.builder and isinstance(request.context, dict):
            files = request.context.get("files")
        return self._parse(request.role, raw, files=files)

    def _build_prompt(self, request: DriverRequest) -> str:
        lines = [f"role: {request.role.value}", f"context: {json.dumps(request.context)}"]
        if request.worktree_path is not None:
            lines.append(f"worktree: {request.worktree_path}")
        if request.role in _COMMAND_ROLES:
            lines.append(git_flags_prompt_line())
        lines.append(_ROLE_OUTPUT_SPEC[request.role])
        return "\n".join(lines)

    def _parse(self, role: Role, raw: str, files: dict | None = None) -> DriverResult:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise DriverParseError(
                f"response is not valid JSON (len={len(raw)}): "
                f"head={raw[:120]!r} tail={raw[-120:]!r}") from e
        if not isinstance(payload, dict):
            raise DriverParseError(f"response must be a JSON object, got {type(payload).__name__}")

        perms = ROLE_PERMISSIONS[role]
        if (payload.get("file_writes") or payload.get("file_edits")) and "write" not in perms:
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
        raw_edits = payload.get("file_edits") or []
        if not isinstance(raw_edits, list):
            raise DriverParseError(
                f"file_edits must be a list of [relpath, old, new] string triples, "
                f"got {type(raw_edits).__name__}")
        file_edits = [self._parse_file_edit(entry, i) for i, entry in enumerate(raw_edits)]
        if file_edits:
            # N9: search/replace edits become full-content writes here, so phases.py
            # and every downstream check (scope, skip gate, checkpoints, verification)
            # see exactly what a whole-file rewrite would have produced
            file_writes += self._apply_file_edits(file_edits, files, {rel for rel, _ in file_writes})
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
    def _parse_file_edit(entry, index: int) -> tuple[str, str, str]:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 3
                or not all(isinstance(x, str) for x in entry)):
            raise DriverParseError(
                f"file_edits[{index}] must be a [relpath, old, new] string triple, "
                f"got {_shape(entry)}")
        return (entry[0], entry[1], entry[2])

    @staticmethod
    def _apply_file_edits(edits: list[tuple[str, str, str]], files: dict | None,
                          written: set[str]) -> list[tuple[str, str]]:
        """Resolve [relpath, old, new] edits into (relpath, full new content) pairs.

        `files` is context.files (relpath -> current text, None for a new file).
        Edits for one relpath apply in order to the progressively edited text; each
        `old` must be non-empty and occur exactly once in the current text. A base
        whose length reaches _MAX_FILE_CHARS was truncated by phases._read_affected
        (no marker), so edits to it are refused: a write resolved against it would
        drop the rest of the file. Paths are compared as Path(rel).as_posix() (like
        _check_scope), so './a.py' and 'a.py' are one file. Errors name the relpath,
        edit index, occurrence count and old/new lengths, never any file text.
        """
        base_files = files if isinstance(files, dict) else {}
        norm_base = {Path(k).as_posix(): v for k, v in base_files.items() if isinstance(k, str)}
        written_norm = {Path(w).as_posix() for w in written}
        texts: dict[str, str] = {}   # normalised key -> progressively edited text
        names: dict[str, str] = {}   # normalised key -> relpath as the builder wrote it
        for i, (rel, old, new) in enumerate(edits):
            key = Path(rel).as_posix()
            if key in written_norm:
                raise DriverParseError(
                    f"file_edits[{i}]: {rel!r} appears in both file_edits and file_writes; use one")
            if key not in texts:
                if rel in base_files:
                    base = base_files[rel]
                elif key in norm_base:
                    base = norm_base[key]
                else:
                    raise DriverParseError(
                        f"file_edits[{i}]: {rel!r} is not in context.files; only files the builder "
                        f"was shown can be edited (new files need file_writes)")
                if not isinstance(base, str):
                    raise DriverParseError(
                        f"file_edits[{i}]: {rel!r} is a new file (null in context.files); "
                        f"use file_writes with the full content")
                if len(base) >= _MAX_FILE_CHARS:
                    raise DriverParseError(
                        f"file_edits[{i}]: {rel!r}: file is truncated in context "
                        f"(>= {_MAX_FILE_CHARS} chars); edits refused - this file cannot be "
                        f"safely changed by the builder")
                texts[key] = base
                names[key] = rel
            if not old:
                raise DriverParseError(
                    f"file_edits[{i}] for {rel!r}: old must be a non-empty string copied from the file")
            n = texts[key].count(old)
            if n != 1:
                raise DriverParseError(
                    f"file_edits[{i}] for {rel!r}: old text occurs {n} times (must be exactly 1); "
                    f"len(old)={len(old)} len(new)={len(new)}")
            texts[key] = texts[key].replace(old, new, 1)
        return [(names[key], text) for key, text in texts.items()]

    @staticmethod
    def _parse_command(entry) -> list[str]:
        # models sometimes send a command as one string; split it (no shell involved \u2014
        # the command runner still applies every metacharacter/allowlist check)
        if isinstance(entry, str) and entry.strip():
            try:
                entry = shlex.split(entry)
            except ValueError as e:
                raise DriverParseError(f"unparseable command string {entry!r}: {e}") from e
        if not isinstance(entry, list) or not entry or not all(isinstance(x, str) for x in entry):
            raise DriverParseError(f"commands entries must be non-empty argv string lists, got {entry!r}")
        return list(entry)
