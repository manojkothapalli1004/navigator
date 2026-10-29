"""Builder test runner (build 2, operator decision 2026-10-28).

run_tests is the ONE in-process MCP tool a tooled builder session (NAVIGATOR_BUILDER_TOOLS
plus NAVIGATOR_BUILDER_TESTS) may call, and the only code path by which a builder can run
anything. It runs the repository's allowlisted pytest entry (the same lookup
phases._run_full_suite uses) plus -q and validated paths under the run directory, inside
the builder's worktree, at most max_runs times per build (one budget, owned by phases, shared by
the first attempt and every repair turn), with the output tail
capped. Every spawn goes through phases._run_sealed and the per-repo CommandRunner, so the
verifier's sealing (no NAVIGATOR_/ACT_/ORC_ env, dead proxies, UV_OFFLINE) and the runner's
argv/allowlist/cwd/timeout rules apply unchanged; nothing here ever spawns outside
_run_sealed. Never a shell, never git, never the network.
"""
import asyncio
from pathlib import Path

from navigator.runtime.phases import _run_sealed
from navigator.safety.errors import CommandRejected

SERVER_NAME = "navigator"
TOOL_NAME = "run_tests"
QUALIFIED_TOOL = "mcp__navigator__run_tests"
OUTPUT_CHARS = 4000
TOOL_DESCRIPTION = (
    "Run the repository's allowlisted pytest command (plus -q) inside your worktree on the "
    "given test paths (relative to the run directory; pass [] for the whole suite - the "
    "argument is required by the schema). Returns 'rc=<returncode>' and the output tail. "
    "Bounded per build; your JSON edits are applied AFTER you reply, so this runs the files "
    "as they are NOW.")

# same test as ClaudeSDKDriver.from_settings' worktree-root guard (defence in depth here)
_USERS_PREFIXES = ("/Users/", "/System/Volumes/Data/Users/")
_SDK_MISSING = ("driver 'claude_sdk' needs the sdk extra: uv sync --extra sdk "
                "(claude-agent-sdk not installed)")


def pytest_entry(allowed_commands) -> list[str] | None:
    """Same lookup as phases._run_full_suite: the first allowlist entry with a "pytest"
    token (split tokens, not substrings), split into argv; None when there is none."""
    for entry in allowed_commands or ():
        if isinstance(entry, str) and "pytest" in entry.split():
            return entry.split()
    return None


def validate_paths(paths, run_dir: Path) -> list[str]:
    """The builder's test paths, unchanged, once each is a non-empty str that does not
    start with "-" (a flag) or "@" (pytest >= 8.2 reads arguments from a file named @file,
    so a file the builder wrote on an earlier turn could smuggle -p / -c / --basetemp=
    /Users/... past the allowlist), is not absolute, has no ".." segment and resolves to
    run_dir or below it. ValueError("rejected path: <p>") otherwise; the message never
    carries the resolved absolute path."""
    if not isinstance(paths, list):
        raise ValueError("rejected path: paths must be a list")
    root = Path(run_dir).resolve()
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str):
            raise ValueError(f"rejected path: {type(p).__name__}")
        if (not p or p.startswith(("-", "@")) or Path(p).is_absolute()
                or ".." in Path(p).parts):
            raise ValueError(f"rejected path: {p[:200]}")
        target = (root / p).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"rejected path: {p[:200]}")
        out.append(p)
    return out


def strip_workdir_prefix(paths, run_dir: str, worktree: str | None):
    """`paths` with a leading "<workdir>/" removed once from each str entry, where workdir is
    `run_dir` relative to `worktree` (posix). The builder's context.test_files are repo-relative
    (git ls-files) while pytest runs in the run directory, so in a repo with workdir "backend"
    "backend/tests/x.py" would resolve to backend/backend/... and waste a budget unit on a
    pytest error. No worktree, a run_dir that is the worktree (".") or one outside it: unchanged.
    A non-list or non-str entry passes through for validate_paths to reject."""
    if worktree is None or not isinstance(paths, list):
        return paths
    try:
        rel = Path(run_dir).resolve().relative_to(Path(worktree).resolve()).as_posix()
    except ValueError:
        return paths
    if rel in (".", ""):
        return paths
    prefix = rel + "/"
    return [p[len(prefix):] if isinstance(p, str) and p.startswith(prefix) else p for p in paths]


def _under_users(path: str) -> bool:
    resolved = str(Path(path).resolve())
    return resolved == "/Users" or resolved.startswith(_USERS_PREFIXES)


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


async def run_tests_handler(args, *, runner, run_dir: str, allowed_commands, budget: dict,
                            max_runs: int, calls: list, lock, worktree: str | None = None,
                            output_chars: int = OUTPUT_CHARS) -> dict:
    """The tool body, SDK-free so it is unit-testable.

    In order: (1) budget - at max_runs calls this build nothing runs; otherwise the call is
    spent BEFORE anything else, so a rejected call spends budget too (looping on rejections
    must end); (2) a run directory under /Users is refused (never reached when
    from_settings' guard ran); (3) no allowlisted pytest entry is refused; (4) a leading
    "<workdir>/" (the run directory relative to `worktree`, when given; context.test_files
    are repo-relative) is stripped once from each path, then the paths are validated;
    (5) the argv runs through _run_sealed in a worker thread, under `lock`
    (the SDK runs each tool call as its own task and phases._environ_replaced is NOT
    re-entrant: two overlapping calls would restore the SEALED environ last and strip the
    daemon's CLAUDE_CODE_OAUTH_TOKEN / API key for every later CLI spawn). Every outcome
    is appended to `calls` as {"argv": argv or None, "text": text[:200]} and returned as
    MCP text content; no exception escapes."""
    def _reply(text: str, argv=None) -> dict:
        calls.append({"argv": argv, "text": text[:200]})
        return _text(text)

    if budget.setdefault("used", 0) >= max_runs:
        return _reply(f"test-run budget exhausted ({max_runs} per build)")
    budget["used"] += 1
    if _under_users(run_dir):
        return _reply("rejected: run directory under /Users")
    entry = pytest_entry(allowed_commands)
    if entry is None:
        return _reply("rejected: no pytest command allowlisted")
    args = args if isinstance(args, dict) else {}
    try:
        paths = validate_paths(strip_workdir_prefix(args.get("paths") or [], run_dir, worktree),
                               Path(run_dir))
    except ValueError as e:
        return _reply(f"rejected: {e}")
    argv = entry + ["-q"] + paths
    async with lock:
        try:
            r = await asyncio.to_thread(_run_sealed, runner, argv, Path(run_dir))
        except CommandRejected as e:
            text = f"rejected: {e}"
        except Exception as e:
            # the SDK's run_tool would turn a raise into an isError result anyway; catching
            # it here keeps the budget accounting and the calls record consistent
            text = f"error: {type(e).__name__}: {e}"[:output_chars]
        else:
            text = f"rc={r.returncode}\n" + (r.stdout + r.stderr)[-output_chars:]
    return _reply(text, argv)


def make_run_tests_server(runner, run_dir: str, allowed_commands, *, max_runs: int,
                          budget: dict | None = None, worktree: str | None = None,
                          output_chars: int = OUTPUT_CHARS):
    """(server_config, calls) for one builder call: an in-process MCP server named
    SERVER_NAME exposing TOOL_NAME, bound to this runner / run_dir / allowlist (and `worktree`,
    for the workdir prefix), counting against `budget` (phases' per-build counter; a fresh one
    when None), with one asyncio.Lock per EVENT LOOP (a Lock binds to the first loop that
    contends it; sdk_query's bounded retry starts a new asyncio.run, and a contended acquire on
    a Lock bound to a closed loop raises RuntimeError). `calls` fills as the tool is used.
    claude_agent_sdk is imported lazily; without the sdk extra this raises the same ValueError
    make_sdk_query_fn raises."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError as e:
        raise ValueError(_SDK_MISSING) from e
    if budget is None:
        budget = {"used": 0}
    calls: list = []
    # one Lock per running loop, keyed by id(loop): a bound Lock holds a reference to its loop,
    # so that id cannot be reused while the entry is live; an unbound one is harmless to reuse
    locks: dict[int, asyncio.Lock] = {}
    allowed = tuple(allowed_commands or ())

    @tool(TOOL_NAME, TOOL_DESCRIPTION, {"paths": list[str]})
    async def run_tests(args):
        loop = asyncio.get_running_loop()
        lock = locks.setdefault(id(loop), asyncio.Lock())
        return await run_tests_handler(args, runner=runner, run_dir=run_dir,
                                       allowed_commands=allowed, budget=budget,
                                       max_runs=max_runs, calls=calls, lock=lock,
                                       worktree=worktree, output_chars=output_chars)

    return create_sdk_mcp_server(SERVER_NAME, tools=[run_tests]), calls
