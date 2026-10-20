import ast
import inspect
import os
import re
import subprocess
import time
from collections import Counter, deque
from contextlib import contextmanager
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
    - which resolve to the worktree root - and "../x" / absolute paths are rejected
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
# Sites are found with ast so aliased imports, unittest decorators, self.skipTest,
# module-level pytestmark and odd spacing are all seen, while text inside strings
# and comments is not (G1 cold audit).
_SKIP_NAMES = frozenset({"skip", "skipif", "importorskip", "skipTest", "skipIf", "skipUnless"})
_SKIP_FALLBACK = re.compile(r"\b(skip|skipif|importorskip|skipTest|skipIf|skipUnless)\s*\(")

def _final_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None

def _is_pytestmark_target(t) -> bool:
    if isinstance(t, ast.Name):
        return t.id == "pytestmark"
    if isinstance(t, (ast.Tuple, ast.List)):
        return any(_is_pytestmark_target(e) for e in t.elts)
    return False

def _skip_sites_by_name(source: str) -> Counter:
    """Skip sites in python source, keyed by kind (skip, skipif, importorskip,
    skipTest, skipIf, skipUnless). Each node is counted once. Unparseable (or too
    deep to parse) source falls back to a text regex."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Counter(m.group(1) for m in _SKIP_FALLBACK.finditer(source))
    counts: Counter = Counter()
    seen: set[int] = set()

    def hit(node, name, *also):
        if id(node) in seen:
            return
        seen.add(id(node))
        seen.update(id(a) for a in also)
        counts[name] += 1

    nodes = list(ast.walk(tree))
    # decorators first, so @x.skipif(...) counts once (not as decorator and call)
    for node in nodes:
        for dec in getattr(node, "decorator_list", None) or []:
            func = dec.func if isinstance(dec, ast.Call) else dec
            name = _final_name(func)
            if name in _SKIP_NAMES:
                hit(dec, name, func)
    for node in nodes:
        if isinstance(node, ast.Call) and id(node) not in seen:
            name = _final_name(node.func)
            if name in _SKIP_NAMES:
                hit(node, name, node.func)
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None or not any(_is_pytestmark_target(t) for t in targets):
            continue
        for sub in ast.walk(value):
            if isinstance(sub, (ast.Name, ast.Attribute)) and id(sub) not in seen:
                name = _final_name(sub)
                if name in _SKIP_NAMES:
                    hit(sub, name)
    return counts

# Resolved detector (G1e-1c): follows imports (at any scope) and `name = expr`
# aliases to the real skip APIs, so `from pytest import skip as s; s()` or
# `m = pytest.mark.skip(...)` then `@m` are seen. It only ever adds to the by-name
# detector: the gate flags growth in either (see _skip_growth).
_SKIP_TARGETS = {"pytest.skip": "skip", "pytest.importorskip": "importorskip",
                 "pytest.mark.skip": "skip", "pytest.mark.skipif": "skipif"}
_SKIP_TARGETS.update({f"{mod}.{name}": name for mod in ("unittest", "unittest.case")
                      for name in ("skip", "skipIf", "skipUnless", "SkipTest")})
# any `<expr>.skipTest` (a bound TestCase method), e.g. `f = self.skipTest; f()`
_ANY_SKIPTEST = "*.skipTest"
_TARGET_KINDS = {**_SKIP_TARGETS, _ANY_SKIPTEST: "skipTest"}
_TARGET_PREFIXES = frozenset(
    ".".join(path.split(".")[:i]) for path in _SKIP_TARGETS
    for i in range(1, path.count(".") + 2)) | {_ANY_SKIPTEST}
# calls that return a decorator: the skip happens where the result is applied
_DECORATOR_FACTORIES = frozenset(
    p for p, kind in _SKIP_TARGETS.items()
    if kind in ("skip", "skipif", "skipIf", "skipUnless") and p != "pytest.skip")
_MAX_BINDING_PATHS = 32
_REQUEUE_FACTOR = 10

def _bind(bindings: dict, name: str, paths) -> bool:
    """Add paths (pruned to target prefixes, capped per set) to name's binding set.
    Never overwrites. True if the set grew."""
    cur = bindings.setdefault(name, set())
    grew = False
    for p in sorted(paths):
        if len(cur) >= _MAX_BINDING_PATHS:
            break
        if p in _TARGET_PREFIXES and p not in cur:
            cur.add(p)
            grew = True
    return grew

def _root_name(node) -> str | None:
    """Name at the root of an expression's Attribute/Call-func spine - the only
    name _resolve reads (e.g. `a` for `a.b(x).c`; call arguments are ignored).
    None if the root is not a Name."""
    while isinstance(node, (ast.Attribute, ast.Call)):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    return node.id if isinstance(node, ast.Name) else None

def _resolve(node, bindings: dict, memo: dict | None = None) -> set:
    """Target paths (or prefixes of them) an expression may evaluate to.

    Walks the Attribute/Call-func spine with an explicit loop, so there is no Python
    recursion proportional to chain depth. With a memo (keyed by node id; only valid
    while bindings don't change) each spine node is resolved once, so resolving every
    call in a long chain stays linear."""
    chain = []
    cur = node
    while True:
        if memo is not None and id(cur) in memo:
            paths = memo[id(cur)]
            break
        if isinstance(cur, ast.Attribute):
            chain.append(cur)
            cur = cur.value
        elif isinstance(cur, ast.Call):
            chain.append(cur)
            cur = cur.func
        else:
            paths = (frozenset(bindings.get(cur.id, ())) if isinstance(cur, ast.Name)
                     else frozenset())
            if memo is not None:
                memo[id(cur)] = paths
            break
    for n in reversed(chain):
        if isinstance(n, ast.Attribute):
            out = {f"{p}.{n.attr}" for p in paths} & _TARGET_PREFIXES
            if n.attr == "skipTest":
                out.add(_ANY_SKIPTEST)
        else:
            # e.g. m = pytest.mark.skip(reason='x'): m stands for the marker
            out = {p for p in paths if p in _TARGET_KINDS}
        paths = frozenset(out)
        if memo is not None:
            memo[id(n)] = paths
    return set(paths)

def _alias_statements(nodes) -> list:
    out = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            out.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif (isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None
              and isinstance(node.target, ast.Name)):
            out.append((node.target.id, node.value))
    return out

def _resolve_aliases(aliases: list, bindings: dict) -> bool:
    """Propagate bindings through aliases with a dependency worklist, so chains
    resolve in whatever order they appear. When a name's set grows, every alias
    whose RHS spine is rooted at it is re-queued (call arguments are not deps:
    _resolve never reads them). Bounded by the per-set cap and a global budget of
    _REQUEUE_FACTOR re-queues per alias statement. Returns True if the budget ran
    out while a re-queue was still needed (the result may be incomplete)."""
    deps: dict = {}
    for i, (_, value) in enumerate(aliases):
        root = _root_name(value)
        if root is not None:
            deps.setdefault(root, []).append(i)
    queue = deque(range(len(aliases)))
    queued = set(queue)
    budget = _REQUEUE_FACTOR * len(aliases)
    exhausted = False
    while queue:
        i = queue.popleft()
        queued.discard(i)
        name, value = aliases[i]
        if _bind(bindings, name, _resolve(value, bindings)):
            for j in deps.get(name, ()):
                if j in queued:
                    continue
                if budget <= 0:
                    exhausted = True
                    continue
                budget -= 1
                queue.append(j)
                queued.add(j)
    return exhausted

def _count_resolved(tree) -> tuple[Counter, bool]:
    """(counts, alias budget exhausted)."""
    nodes = list(ast.walk(tree))
    bindings: dict = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    _bind(bindings, a.asname, {a.name})
                else:
                    top = a.name.split(".")[0]
                    _bind(bindings, top, {top})
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                if a.name != "*":
                    _bind(bindings, a.asname or a.name, {f"{node.module}.{a.name}"})
    aliases = _alias_statements(nodes)
    exhausted = _resolve_aliases(aliases, bindings)

    counts: Counter = Counter()
    seen: set[int] = set()
    memo: dict = {}  # bindings are final from here on

    def kinds(expr) -> list:
        return sorted({_TARGET_KINDS[p] for p in _resolve(expr, bindings, memo)
                       if p in _TARGET_KINDS})

    def hit(node, found, *also):
        if not found or id(node) in seen:
            return
        seen.add(id(node))
        seen.update(id(a) for a in also)
        for k in found:
            counts[k] += 1

    # module-level pytestmark = <marker or list of markers>
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None or not any(_is_pytestmark_target(t) for t in targets):
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Call):
                hit(sub, kinds(sub.func), sub.func)
            elif isinstance(sub, (ast.Name, ast.Attribute)):
                hit(sub, kinds(sub))
    # m = pytest.mark.skip(...) only builds a marker; each use of m is counted instead
    loaded = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for name, value in aliases:
        if name == "pytestmark" or name not in loaded or not isinstance(value, ast.Call):
            continue
        paths = {p for p in _resolve(value.func, bindings, memo) if p in _TARGET_KINDS}
        if paths and paths <= _DECORATOR_FACTORIES and id(value) not in seen:
            seen.add(id(value))
            seen.add(id(value.func))
    for node in nodes:
        for dec in getattr(node, "decorator_list", None) or []:
            func = dec.func if isinstance(dec, ast.Call) else dec
            hit(dec, kinds(func), func)
    for node in nodes:
        if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Name, ast.Attribute)):
            hit(node.exc, kinds(node.exc))
    for node in nodes:
        if isinstance(node, ast.Call) and id(node) not in seen:
            hit(node, kinds(node.func), node.func)
    return counts, exhausted

def _resolved_analysis(source: str) -> tuple[Counter, str | None]:
    """(resolved skip sites, reason the analysis is incomplete or None).
    Reasons: 'budget' (alias budget ran out; counts are a lower bound),
    'recursion' / 'memory' (source too deep to analyse; counts are empty).
    Unparseable source is not incomplete: the interpreter can't run it either, and
    the by-name detector's regex fallback still applies."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return Counter(), None
    except RecursionError:
        return Counter(), "recursion"
    except MemoryError:
        return Counter(), "memory"
    try:
        counts, exhausted = _count_resolved(tree)
    except RecursionError:
        return Counter(), "recursion"
    except MemoryError:
        return Counter(), "memory"
    return counts, ("budget" if exhausted else None)

def _skip_sites_resolved(source: str) -> Counter:
    """Skip sites whose callee, decorator or raised exception resolves (through
    imports and `name = expr` aliases) to a pytest/unittest skip API, keyed by kind.
    Unparseable or pathologically deep source yields an empty Counter; the gate
    uses _resolved_analysis to fail closed on the latter."""
    return _resolved_analysis(source)[0]

def _skip_sites(source: str) -> Counter:
    """Per-kind max of the by-name and resolved detectors (display/other callers)."""
    return _skip_sites_by_name(source) | _skip_sites_resolved(source)

def _skip_gate(new: str, old: str) -> tuple[Counter, str | None]:
    """(growth, reason the NEW file's analysis is incomplete or None). An
    incomplete analysis of the old file keeps the old behaviour (no resolved sites)."""
    new_resolved, incomplete = _resolved_analysis(new)
    grown = ((_skip_sites_by_name(new) - _skip_sites_by_name(old))
             | (new_resolved - _skip_sites_resolved(old)))
    return grown, incomplete

def _skip_growth(new: str, old: str) -> Counter:
    """Growth is flagged if EITHER detector grows (Counter union = per-kind max of
    the two deltas). Taking the max of the detectors per side first would let a site
    seen only by name hide behind resolved-only sites already in the old file."""
    return _skip_gate(new, old)[0]

def _skip_gated(rel: str) -> bool:
    # conftest.py can skip whole suites and pytest also collects *_test.py; both are
    # gated here only. _is_test_file stays narrower because it widens builder scope.
    p = Path(rel)
    return (_is_test_file(rel) or p.name == "conftest.py"
            or (p.suffix == ".py" and p.name.endswith("_test.py")))

def _added_skips(wt: Path, writes: list[tuple[str, str]]) -> list[str]:
    """'<file>: <n>' for each written test file (or conftest.py) where any kind of
    skip site grew versus the file on disk before the write (empty for new files),
    or '<file>: skip analysis incomplete (<reason>)' when the new content could not
    be fully analysed (fail closed). Must run before writing."""
    out = []
    for rel, content in writes:
        if not _skip_gated(rel):
            continue
        target = _resolve_write_target(wt, rel)
        original = target.read_text(errors="replace") if target.is_file() else ""
        grown, incomplete = _skip_gate(content, original)
        if incomplete:
            out.append(f"{rel}: skip analysis incomplete ({incomplete})")
        elif grown:
            out.append(f"{rel}: {sum(grown.values())}")
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

# S3b/S3c: verifier commands execute builder-written test code (arbitrary Python) on
# the same host as ACT/ORC. The Navigator process holds their URLs and tokens in its
# environment; a verifier subprocess must not inherit them, and must not be able to
# reach the local services through a proxy it picks up from the environment either.
# This is defence in depth, not a sandbox: a determined test can still open a raw
# socket to 127.0.0.1:8090, and HOME stays readable. It removes the easy path
# (credentials and discovery) and makes any such attempt deliberate and visible in
# the diff.
_SEALED_PREFIXES = ("NAVIGATOR_", "ACT_", "ORC_")
_SEALED_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")
_SEALED_EXACT = frozenset({"TOKEN"})
_DEAD_PROXY = "http://127.0.0.1:9"
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")
# `uv run pytest` behind the dead proxy would otherwise retry the network for every
# wheel it cannot find before giving up; offline mode makes uv use its cache (which
# already works warm) and fail fast and clearly when it cannot.
_OFFLINE_KEYS = {"UV_OFFLINE": "1"}

def _is_sealed_key(key: str) -> bool:
    """Keys the sealed env drops (case-insensitive): Navigator/ACT/ORC addresses
    (NAVIGATOR_*, ACT_*, ORC_*) and common credential names (*_TOKEN, *_SECRET,
    *_PASSWORD, *_API_KEY, TOKEN). Prefix/suffix matches, not substring ones, so
    CONTACT_*, EXACT_* and TOKENIZERS_PARALLELISM are kept."""
    upper = key.upper()
    return (upper.startswith(_SEALED_PREFIXES) or upper.endswith(_SEALED_SUFFIXES)
            or upper in _SEALED_EXACT)

def sealed_verifier_env(base: dict | None = None) -> dict[str, str]:
    """Environment for verifier subprocesses: os.environ (or `base`) with the
    Navigator/ACT/ORC addresses and common credential names removed (see
    _is_sealed_key), HTTP(S) proxies pointed at a dead port (9, discard), NO_PROXY
    cleared so the dead proxy applies to localhost too, and UV_OFFLINE=1 so uv
    fails fast instead of retrying the network. Not a sandbox: PATH, HOME (which
    stays readable, dotfiles included) and everything else are kept."""
    src = os.environ if base is None else base
    env: dict[str, str] = {}
    for key, value in src.items():
        if _is_sealed_key(key):
            continue
        env[key] = value
    for key in _PROXY_KEYS:
        env[key] = _DEAD_PROXY
    for key in _NO_PROXY_KEYS:
        env[key] = ""
    env.update(_OFFLINE_KEYS)
    return env

def _accepts_env(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "env" or p.kind is p.VAR_KEYWORD for p in params)

@contextmanager
def _environ_replaced(env: dict[str, str]):
    """Temporarily make os.environ exactly `env`, so a runner that spawns with the
    default (inherited) environment spawns with the sealed one. Navigator is
    single-flight, so nothing else reads os.environ meanwhile."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)

def _run_sealed(runner, argv: list[str], cwd: Path):
    """The one place verifier commands are spawned. Passes the sealed env explicitly
    when the runner accepts `env=`, and replaces os.environ for the duration of the
    call either way (a runner that inherits the process environment - like
    CommandRunner, which derives its child env from os.environ - then inherits the
    sealed one). CommandRejected propagates unchanged."""
    env = sealed_verifier_env()
    with _environ_replaced(env):
        if _accepts_env(runner.run):
            return runner.run(argv, cwd=cwd, env=env)
        return runner.run(argv, cwd=cwd)

def run_build_and_verify(driver: SubagentDriver, worktree: WorktreeHandle,
                         run_id: str, runner: CommandRunner,
                         proposal: dict | None = None,
                         breakers: CircuitBreakers | None = None,
                         started_monotonic: float | None = None,
                         workdir: str = ".") -> PhaseResult:
    res = PhaseResult()
    wt = Path(worktree.path)
    run_dir = run_dir_for(wt, workdir)
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
    # verifier - a verify phase with no commands is a failure, not a silent pass
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
            r = _run_sealed(runner, argv, run_dir)
        except CommandRejected as e:
            # a disallowed command is a verification result, not a crash
            res.verify_returncode = 126
            res.verify_output = f"verifier command rejected: {e}"
            res.verify_log.append({"argv": " ".join(argv), "argv_list": list(argv),
                                   "returncode": 126,
                                   "output_tail": res.verify_output[-1500:]})
            if breakers:
                breakers.record_failure(f"rejected:{argv[0]}")
            break
        res.verify_returncode = r.returncode
        res.verify_output = r.stdout + r.stderr
        res.verify_log.append({"argv": " ".join(argv), "argv_list": list(argv),
                               "returncode": r.returncode,
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
        r = _run_sealed(runner, argv, run_dir)
        rc, out = r.returncode, r.stdout + r.stderr
    except CommandRejected as e:
        rc, out = 126, f"full-suite command rejected: {e}"
    res.verify_log.append({"argv": " ".join(argv), "argv_list": list(argv), "returncode": rc,
                           "output_tail": out[-1500:]})
    res.verify_returncode, res.verify_output = rc, out
    if rc != 0 and breakers:
        m = _FAILED_TESTS.search(out)
        if m:
            breakers.check_failed_tests(int(m.group(1)))


def run_dir_for(wt: Path, workdir: str) -> Path:
    """Where verifier commands run: the repo's workdir inside the worktree (validated to
    stay inside it), or the worktree itself when workdir is '.' or ''."""
    wt = Path(wt)
    return _resolve_write_target(wt, workdir) if workdir not in (".", "") else wt


def reverify(runner, run_dir: Path, argvs: list[list[str]], breakers=None,
             changed_files: list[str] | None = None) -> PhaseResult:
    """Re-run a run's recorded verification commands in `run_dir` (N12d: a rebased
    sibling is re-verified with the commands that verified it; no driver call). Same
    per-command loop as run_build_and_verify: stop at the first non-zero return code, a
    rejected command is rc 126, and no commands at all is a failure (rc 1). When the
    rebased change (`changed_files`, repo-relative) is a python change in a repo with a
    test suite, the full suite runs afterwards unless it is already among `argvs`."""
    res = PhaseResult()
    run_dir = Path(run_dir)
    argvs = [list(a) for a in argvs if a]
    if not argvs:
        res.verify_returncode = 1
        res.verify_output = "no recorded verification commands to re-run"
        return res
    for argv in argvs:
        try:
            r = _run_sealed(runner, argv, run_dir)
        except CommandRejected as e:
            res.verify_returncode = 126
            res.verify_output = f"verifier command rejected: {e}"
            res.verify_log.append({"argv": " ".join(argv), "argv_list": list(argv),
                                   "returncode": 126,
                                   "output_tail": res.verify_output[-1500:]})
            if breakers:
                breakers.record_failure(f"rejected:{argv[0]}")
            break
        res.verify_returncode = r.returncode
        res.verify_output = r.stdout + r.stderr
        res.verify_log.append({"argv": " ".join(argv), "argv_list": list(argv),
                               "returncode": r.returncode,
                               "output_tail": res.verify_output[-1500:]})
        if r.returncode != 0:
            if breakers:
                m = _FAILED_TESTS.search(res.verify_output)
                if m:
                    breakers.check_failed_tests(int(m.group(1)))
                breakers.record_failure(" ".join(argv))
            break
    if res.verify_returncode == 0 and _requires_tests(run_dir, list(changed_files or [])):
        entry = next((a for a in runner.allowlist if "pytest" in a.split()), None)
        if entry is None or entry.split() + ["-q"] not in argvs:
            _run_full_suite(runner, run_dir, res, breakers)
    return res
