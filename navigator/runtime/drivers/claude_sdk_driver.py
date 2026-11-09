import hashlib
import inspect
import json
import os
import shlex
import stat
from pathlib import Path
from typing import Callable
from navigator.config import Settings
from navigator.runtime.drivers.base import ROLE_PERMISSIONS, DriverRequest, DriverResult
from navigator.runtime.drivers.builder_tools import QUALIFIED_TOOL, make_run_tests_server
# N9b: phases._read_affected cuts context.files at this many chars with no marker; an
# edit resolved against such a base would silently drop the rest of the file
from navigator.runtime.phases import _MAX_FILE_CHARS, _is_large, _large_digest, _is_test_file
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import git_flag_allowlist_text
from navigator.safety.errors import DriverParseError

class _EditApplyError(DriverParseError):
    """A file_edits entry that could not be applied (absent/duplicate old, unloadable file).
    rel, index and old ride as attributes for the repair turn, never in the message."""
    def __init__(self, message: str, rel: str | None = None, index: int | None = None,
                 old: str | None = None):
        super().__init__(message)
        self.rel, self.index, self.old = rel, index, old


class _UnsafeRead(Exception):
    """_safe_read refused a path; the message carries no file text."""


def _safe_read(wt, rel) -> tuple[str, bytes, tuple[int, int]]:
    """(text, raw bytes, (st_dev, st_ino)) of an existing regular, single-link, LF-only UTF-8
    file at wt/rel. No symlink in any component, nothing outside the worktree, no .git component,
    every component's case must match the directory listing. Raises _UnsafeRead otherwise."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or Path(rel).is_absolute():
        raise _UnsafeRead("bad path")
    parts = Path(rel).parts
    if not parts or ".." in parts or ".git" in parts:
        raise _UnsafeRead("path not allowed")
    try:
        wt_real = os.path.realpath(str(wt))
        target = os.path.join(wt_real, *parts)
        if os.path.realpath(target) != target:
            raise _UnsafeRead("symlink or non-canonical path")
        cur = wt_real
        for part in parts:
            if part not in os.listdir(cur):
                raise _UnsafeRead("path case mismatch")
            cur = os.path.join(cur, part)
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _UnsafeRead("not a plain regular file")
        with open(target, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, ValueError) as e:     # UnicodeDecodeError is a ValueError
        raise _UnsafeRead("unreadable") from e
    if "\r" in text:
        raise _UnsafeRead("CRLF line endings")
    return text, raw, (st.st_dev, st.st_ino)


def _load_unshown(wt, rel, affected) -> tuple[str, tuple[int, int]]:
    """Disk text of an existing in-scope file the builder was not shown (spec Part C): in
    affected_files or a test file by phases._is_test_file, <= _MAX_FILE_CHARS, plus every
    _safe_read rule. Returns (text, inode id); raises _UnsafeRead."""
    if not isinstance(rel, str) or not rel:
        raise _UnsafeRead("bad path")
    if Path(rel).as_posix() not in (affected or ()) and not _is_test_file(rel):
        raise _UnsafeRead("not in scope")
    text, _raw, ident = _safe_read(wt, rel)
    if len(text) > _MAX_FILE_CHARS:
        raise _UnsafeRead("file too large to edit unshown")
    return text, ident


def _norm_large(large) -> dict:
    return ({Path(k).as_posix(): v for k, v in large.items() if isinstance(k, str) and isinstance(v, dict)}
            if isinstance(large, dict) else {})


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
                   'Make the smallest change that solves the task: no refactors, renames or '
                   'reformatting. Add tests to the EXISTING test file for that module when there is '
                   'one (e.g. tests/test_recipes.py), with at most one or two focused test '
                   'functions; create a new test file only if none exists. Match the code\'s '
                   'existing style and error messages. '
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
                   'A file listed in context.large_files is shown as an outline plus excerpts; use '
                   'file_edits only, with old copied verbatim from an excerpt (or from Read if you have '
                   'a Read tool: offset/limit) and unique in the whole file; if unsure, include more '
                   'surrounding lines. '
                   'You may also edit an existing file not in context.files if it is a test file or is '
                   'listed in affected_files; old must occur exactly once in the real file. '
                   'Only write paths listed in context.files, plus test files; when context.editable_dir '
                   'is set you may instead edit or write any file under that directory (find it with '
                   'your tools) and nothing outside it. '
                   'If the repo has tests, add or update tests that exercise the new or changed '
                   'behaviour (a passing suite that never runs the new path proves nothing). '
                   'You MAY add new test files not in context.files; follow the layout and '
                   'naming of context.test_files. context.related_files are for reference: use them '
                   'to get models, fixtures and APIs right. Never write non-test related files; '
                   'DO update existing test files among them when your change alters an API '
                   'they call. A test file in context.files that covers the changed module is the '
                   'place for new tests; prefer file_edits on it over a new file. If a run_tests tool is available, its result describes the worktree '
                   'BEFORE your edits; still reply with the full JSON object.'),
    Role.verifier: ('Choose commands that prove context.proposal works, each starting with an entry '
                    'of context.allowed_commands. Commands run inside context.workdir; use paths '
                    'relative to it (context.changed_files already are). Respond with ONLY a JSON '
                    'object: {"commands": [[argv...], ...], "summary": "..."}'),
}

_COMMAND_ROLES = (Role.builder, Role.verifier)

# the builder's read-only tool set when Settings.builder_tools is on (design 2026-10-28)
BUILDER_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")


def _accepts_cwd(fn) -> bool:
    # True if fn takes a `cwd` keyword (or **kwargs); the many (prompt)-only query fns do not
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "cwd" or p.kind is p.VAR_KEYWORD for p in params)


def _accepts_mcp_servers(fn) -> bool:
    # True if fn takes an `mcp_servers` keyword (or **kwargs): only then is the run_tests
    # server built for a builder call
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "mcp_servers" or p.kind is p.VAR_KEYWORD for p in params)


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


def _with_editable(files, raw: str, wt: Path, editable_dir: str, large: dict | None = None) -> dict:
    """context.files plus the worktree text of every file_edits path that resolves to an
    existing regular file inside wt/editable_dir (site-checker proposals name no files,
    so the builder finds them with its tools). Anything else stays unknown and is
    refused by _apply_file_edits as before; malformed responses are left to _parse."""
    out = dict(files) if isinstance(files, dict) else {}
    try:
        edits = json.loads(raw).get("file_edits") or []
        root = (wt / editable_dir).resolve()
    except (ValueError, TypeError, AttributeError, OSError):
        return out
    for entry in edits if isinstance(edits, list) else []:
        rel = entry[0] if isinstance(entry, (list, tuple)) and entry else None
        if not isinstance(rel, str) or not rel or rel in out or Path(rel).is_absolute():
            continue
        target = wt / rel
        try:
            real = target.resolve()
            if target.is_symlink() or not real.is_relative_to(root) or not real.is_file():
                continue
            data = real.read_bytes()
            text = data.decode()
            if _is_large(text):
                # same declaration phases makes: digest in context, edits resolved on disk
                out[rel] = _large_digest(rel, text)
                if large is not None:
                    large[rel] = {"chars": len(text), "lines": text.count("\n") + 1,
                                  "sha256": hashlib.sha256(data).hexdigest()}
            else:
                out[rel] = real.read_text()
        except (OSError, ValueError):
            # NUL bytes, over-long names, unreadable or non-UTF-8 files: left unknown
            continue
    return out


class ClaudeSDKDriver:
    """SubagentDriver backed by an injectable LLM query function.

    query_fn is (prompt: str) -> str so tests run offline; a builder query_fn may
    also accept cwd=; the real claude-agent-sdk binding is supplied at wiring time
    (sdk extra), never imported here.
    """
    def __init__(self, *, query_fn: Callable[[str], str], budget_guard: BudgetGuard,
                 role_query_fns: dict[Role, Callable[[str], str]] | None = None,
                 estimated_cost_per_call_usd: float = 0.25,
                 builder_tests: bool = False, builder_test_runs: int = 5):
        self.query_fn = query_fn
        self.role_query_fns = role_query_fns or {}
        self.budget_guard = budget_guard
        self.estimated_cost_per_call_usd = estimated_cost_per_call_usd
        # build 2: when on, a builder request that carries a runner and run_dir gets the
        # run_tests MCP tool (builder_tools.make_run_tests_server), at most builder_test_runs
        # calls per BUILD (the request's test_budget: one counter shared by the first attempt and
        # every repair turn); last_test_runs is the calls record of the most recent builder call
        self.builder_tests = builder_tests
        self.builder_test_runs = builder_test_runs
        self.last_test_runs: list = []

    @classmethod
    def from_settings(cls, settings: Settings, *, budget_guard: BudgetGuard,
                      make_query_fn: Callable[..., Callable[[str], str]] | None = None,
                      estimated_cost_per_call_usd: float = 0.25,
                      cooldowns: dict | None = None) -> "ClaudeSDKDriver":
        """Wire the live driver from Settings: per-role models (cheaper models where
        the role doesn't need the strongest one), the model fallback chain and ONE
        cooldown dict shared by every query fn, so a model that ran out of credits
        for the builder is skipped by the other roles too.

        make_query_fn is injectable (default: make_sdk_query_fn) so the wiring is
        testable without the sdk extra. `cooldowns` (U2a settings hot reload): when given,
        that SAME dict object is used instead of a fresh one, so a driver rebuilt after a
        settings change keeps the models already cooling. The dict used is exposed as
        driver.cooldowns.
        """
        if make_query_fn is None:
            from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
            make_query_fn = make_sdk_query_fn
        if cooldowns is None:
            cooldowns = {}
        common = dict(timeout_s=settings.sdk_timeout_s,
                      fallbacks=tuple(settings.sdk_model_fallbacks),
                      cooldown_s=settings.sdk_model_cooldown_s,
                      cooldowns=cooldowns)
        role_fns = {Role(role): make_query_fn(model=model, **common)
                    for role, model in settings.sdk_role_models.items()}
        if settings.builder_tools:
            # the deny list blocks /Users/**: a worktree root there would deny the builder its own
            # files and burn every turn, so refuse at wiring time instead of failing silently per run
            root = str(Path(settings.worktree_root).resolve())
            if root == "/Users" or root.startswith(("/Users/", "/System/Volumes/Data/Users/")):
                raise ValueError("NAVIGATOR_BUILDER_TOOLS needs NAVIGATOR_WORKTREE_ROOT outside /Users "
                                 "(the builder's deny list blocks /Users/**); got "
                                 + settings.worktree_root)
            # the builder (and only the builder) gets read-only tools; it keeps its role model
            # (NAVIGATOR_SDK_MODEL_BUILDER, else the default model), the same fallback chain and
            # the SAME cooldown dict; it never gets less time than a tool-less query
            builder_model = settings.sdk_role_models.get("builder", settings.sdk_model)
            role_fns[Role.builder] = make_query_fn(
                model=builder_model, tools=BUILDER_TOOLS, max_turns=settings.sdk_builder_max_turns,
                timeout_s=max(settings.sdk_timeout_s, settings.sdk_builder_timeout_s),
                fallbacks=common["fallbacks"], cooldown_s=common["cooldown_s"], cooldowns=cooldowns)
        if settings.builder_tests and not settings.builder_tools:
            # the /Users guard above ran first: builder_tests is never wired for a worktree root
            # under /Users, and never without the tooled session it rides on
            raise ValueError("NAVIGATOR_BUILDER_TESTS needs NAVIGATOR_BUILDER_TOOLS=1 (the run_tests tool rides on "
                             "the tooled builder session)")
        driver = cls(query_fn=make_query_fn(model=settings.sdk_model, **common),
                     role_query_fns=role_fns, budget_guard=budget_guard,
                     estimated_cost_per_call_usd=estimated_cost_per_call_usd,
                     builder_tests=settings.builder_tests,
                     builder_test_runs=settings.builder_test_runs)
        driver.cooldowns = cooldowns
        return driver

    def _query_kwargs(self, request: DriverRequest, query_fn) -> dict:
        """Keyword args for query_fn, built ONCE per run_role so a repair turn reuses the same
        run_tests server and calls record (the per-build test_budget is never reset)."""
        if request.role == Role.builder and request.worktree_path is not None and _accepts_cwd(query_fn):
            # tooled builder: the SDK session's cwd is the run worktree (read-only tools stay inside it)
            if (self.builder_tests and request.runner is not None and request.run_dir
                    and _accepts_mcp_servers(query_fn)):
                # build 2: one in-process MCP tool, run_tests, bounded per build (request.test_budget
                # is phases' per-build counter; None keeps a fresh per-call budget); it runs the
                # allowlisted pytest entry through phases._run_sealed and the repo's runner. The
                # worktree path lets the tool strip a repo-relative "<workdir>/" prefix (2b)
                server, calls = make_run_tests_server(request.runner, request.run_dir,
                                                      request.allowed_commands,
                                                      max_runs=self.builder_test_runs,
                                                      budget=request.test_budget,
                                                      worktree=request.worktree_path)
                self.last_test_runs = calls
                return {"cwd": request.worktree_path, "mcp_servers": {"navigator": server},
                        "mcp_tools": (QUALIFIED_TOOL,)}
            return {"cwd": request.worktree_path}
        return {}

    def run_role(self, request: DriverRequest) -> DriverResult:
        self.budget_guard.check_before_call(self.estimated_cost_per_call_usd)
        query_fn = self.role_query_fns.get(request.role, self.query_fn)
        prompt = self._build_prompt(request)
        kwargs = self._query_kwargs(request, query_fn)
        raw = query_fn(prompt, **kwargs)
        self.budget_guard.record_spend(self.estimated_cost_per_call_usd)
        try:
            return self._parse_reply(request, raw)
        except _EditApplyError as e:
            # ONE repair turn (spec 2026-11-08): the same call, the error and an excerpt of the
            # current file; any error from the second parse propagates unchanged
            self.budget_guard.check_before_call(self.estimated_cost_per_call_usd)
            raw2 = query_fn(self._repair_prompt(request, prompt, raw, e), **kwargs)
            self.budget_guard.record_spend(self.estimated_cost_per_call_usd)
            return self._parse_reply(request, raw2)

    def _parse_reply(self, request: DriverRequest, raw: str) -> DriverResult:
        # N9: the builder's file_edits are resolved against the file contents it was
        # shown (context.files: relpath -> current text, None for a new file)
        files, large, wt, affected = None, {}, None, set()
        if request.role == Role.builder and isinstance(request.context, dict):
            files = request.context.get("files")
            large = dict(request.context.get("large_files") or {})
            prop = request.context.get("proposal")
            aff = prop.get("affected_files") if isinstance(prop, dict) else None
            affected = ({Path(a).as_posix() for a in aff if isinstance(a, str)}
                        if isinstance(aff, list) else set())
            wt = Path(request.worktree_path) if request.worktree_path else None
            editable = request.context.get("editable_dir")
            if isinstance(editable, str) and editable and wt:
                files = _with_editable(files, raw, wt, editable, large)
        return self._parse(request.role, raw, files=files, large=large, wt=wt, affected=affected)

    @staticmethod
    def _repair_excerpt(request: DriverRequest, err: _EditApplyError) -> str:
        """Verbatim lines of the CURRENT file near the failed edit, only for a path that passes
        the same gate as an edit (shown, large, affected, test file, or under editable_dir) and
        _safe_read. Model-only: never logged, never in an exception."""
        ctx = request.context if isinstance(request.context, dict) else {}
        if not err.rel or not err.old or not request.worktree_path:
            return ""
        key = Path(err.rel).as_posix()
        prop = ctx.get("proposal")
        aff = prop.get("affected_files") if isinstance(prop, dict) else None
        affected = {Path(a).as_posix() for a in aff if isinstance(a, str)} if isinstance(aff, list) else set()
        shown = {Path(k).as_posix() for k in (ctx.get("files") or {}) if isinstance(k, str)}
        editable = ctx.get("editable_dir")
        in_editable = isinstance(editable, str) and bool(editable) and key.startswith(editable.rstrip("/") + "/")
        if not (key in shown or key in _norm_large(ctx.get("large_files")) or key in affected
                or _is_test_file(key) or in_editable):
            return ""
        try:
            text, _raw, _ident = _safe_read(Path(request.worktree_path), key)
        except _UnsafeRead:
            return ""
        first = err.old.split("\n", 1)[0]
        pos = text.find(first) if first.strip() else -1
        if pos < 0:
            for ln in sorted(err.old.split("\n"), key=len, reverse=True):
                if ln.strip() and ln in text:
                    pos = text.find(ln)
                    break
        if pos < 0:
            return ""
        lines = text.split("\n")
        centre = text.count("\n", 0, pos)
        start = max(0, centre - 30)
        body = "\n".join(lines[start:start + 60])[:4000]
        return f"\ncurrent text of {key} near the failed edit (lines {start + 1}-{min(len(lines), start + 60)}):\n{body}"

    def _repair_prompt(self, request: DriverRequest, prompt: str, raw: str, err: _EditApplyError) -> str:
        return (f"{prompt}\nyour previous reply:\n{raw[:20000]}\n"
                f"edit-application error (fix only the edits; reply with the full JSON object again): "
                f"{err}{self._repair_excerpt(request, err)}")

    def _build_prompt(self, request: DriverRequest) -> str:
        lines = [f"role: {request.role.value}", f"context: {json.dumps(request.context)}"]
        if request.worktree_path is not None:
            lines.append(f"worktree: {request.worktree_path}")
        if request.role in _COMMAND_ROLES:
            lines.append(git_flags_prompt_line())
        lines.append(_ROLE_OUTPUT_SPEC[request.role])
        return "\n".join(lines)

    def _parse(self, role: Role, raw: str, files: dict | None = None, large: dict | None = None,
               wt: Path | None = None, affected: set | None = None) -> DriverResult:
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
        norm_large = _norm_large(large)
        for i, (wrel, _c) in enumerate(file_writes):
            if Path(wrel).as_posix() in norm_large:
                raise DriverParseError(
                    f"file_writes[{i}]: {wrel} is a large file the builder only saw in part; "
                    f"use file_edits")
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
            file_writes += self._apply_file_edits(file_edits, files, {rel for rel, _ in file_writes},
                                                 large=large, wt=wt, affected=affected)
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
                          written: set[str], large: dict | None = None,
                          wt: Path | None = None, affected: set | None = None) -> list[tuple[str, str]]:
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
        norm_large = _norm_large(large)
        idents: dict[tuple[int, int], str] = {}   # disk-loaded inode -> the key that loaded it
        texts: dict[str, str] = {}   # normalised key -> progressively edited text
        names: dict[str, str] = {}   # normalised key -> relpath as the builder wrote it
        for i, (rel, old, new) in enumerate(edits):
            key = Path(rel).as_posix()
            if key in written_norm:
                raise DriverParseError(
                    f"file_edits[{i}]: {rel!r} appears in both file_edits and file_writes; use one")
            if key not in texts and key in norm_large:
                # a large file: files[rel] holds the digest, never a base. The base is the real
                # disk text, valid only if it is still the file the context described
                info = norm_large[key]
                if wt is None:
                    raise _EditApplyError(f"file_edits[{i}]: {rel!r}: large file cannot be read; "
                                          f"edit refused", rel, i, old)
                try:
                    text, raw, ident = _safe_read(wt, key)
                except _UnsafeRead as e:
                    raise _EditApplyError(f"file_edits[{i}]: {rel!r}: large file cannot be read safely "
                                          f"({e}); edit refused", rel, i, old) from e
                if (len(text) != info.get("chars") or not info.get("sha256")
                        or hashlib.sha256(raw).hexdigest() != info["sha256"]):
                    raise _EditApplyError(f"file_edits[{i}]: {rel!r}: large file changed since the "
                                          f"context was built; edit refused", rel, i, old)
                if ident in idents:
                    raise _EditApplyError(f"file_edits[{i}]: {rel!r} is the same file as "
                                          f"{idents[ident]!r}; edit refused", rel, i, old)
                idents[ident] = key
                texts[key] = text
                names[key] = rel
            elif key not in texts:
                if rel in base_files:
                    base = base_files[rel]
                elif key in norm_base:
                    base = norm_base[key]
                elif wt is not None:
                    try:
                        base, ident = _load_unshown(wt, key, affected)
                    except _UnsafeRead as e:
                        raise _EditApplyError(
                            f"file_edits[{i}]: {rel!r} is not in context.files and cannot be loaded "
                            f"from disk ({e}); only an existing test file or a file in affected_files "
                            f"under {_MAX_FILE_CHARS} chars can be edited unshown (new files need "
                            f"file_writes)", rel, i, old) from e
                    if ident in idents:
                        raise _EditApplyError(f"file_edits[{i}]: {rel!r} is the same file as "
                                              f"{idents[ident]!r}; edit refused", rel, i, old)
                    idents[ident] = key
                else:
                    raise _EditApplyError(
                        f"file_edits[{i}]: {rel!r} is not in context.files; only files the builder "
                        f"was shown can be edited (new files need file_writes)", rel, i, old)
                if not isinstance(base, str):
                    raise DriverParseError(
                        f"file_edits[{i}]: {rel!r} is a new file (null in context.files); "
                        f"use file_writes with the full content")
                if len(base) >= _MAX_FILE_CHARS:
                    raise _EditApplyError(
                        f"file_edits[{i}]: {rel!r}: file is truncated in context "
                        f"(>= {_MAX_FILE_CHARS} chars); edits refused - this file cannot be "
                        f"safely changed by the builder", rel, i, old)
                texts[key] = base
                names[key] = rel
            if not old:
                raise DriverParseError(
                    f"file_edits[{i}] for {rel!r}: old must be a non-empty string copied from the file")
            n = texts[key].count(old)
            if n != 1:
                raise _EditApplyError(
                    f"file_edits[{i}] for {rel!r}: old text occurs {n} times (must be exactly 1); "
                    f"len(old)={len(old)} len(new)={len(new)}", rel, i, old)
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
