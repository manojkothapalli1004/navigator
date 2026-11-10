"""Reviewer memory B1: a per-repo memory log OUTSIDE the repos (operator design 2026-11-04).

Declines with their reason, failed attempts and audit findings leave three disjoint traces
today (Outcome.reason_code/lessons, ORC memory notes, the run.audited payload) and none is
keyed by repo or file. This module appends one compact JSON line per event to
<root>/<repo basename>.jsonl so a later build (B3) can tell a builder "this file was declined
twice for X". Writers (executor._write_outcome, executor.decline_run, cli._audited) are
best-effort: remember() never raises, a failure is one structlog warning.

Switch: Settings.repo_memory (NAVIGATOR_REPO_MEMORY), off by default. Root: Settings.repo_memory_root
(NAVIGATOR_REPO_MEMORY_ROOT), else DEFAULT_ROOT - resolved by root_for() at CALL time, exactly like
credits.events_path(), so tests/conftest.py can point every fallback under tmp.

Entry: {"ts": UTC ISO seconds, "kind": decline|failure|audit, "repo": full path, "run_id",
"proposal_id", "reason_code", "text": <= 2000 chars, "files": affected_files first (sorted) then the
other touched files (sorted), <= 50 in all, "source": executor|operator|telegram|auditor,
"verified_sha", "base_sha", "title"}. Append pattern copied from credits._event: directory 0700, file
opened O_WRONLY|O_CREAT|O_APPEND with mode 0600, one write() per line, so the daemon and the CLI may
append concurrently without tearing a line. Accepted risk: two repos with one basename share a file
(the entry's "repo" disambiguates).

B3 (2026-11-10): compact() is the one reader view - executor._memory_context hands its result to
run_build_and_verify(repo_memory=...) and phases puts it in builder and verifier context.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from navigator.observability.logging import get_logger

DEFAULT_ROOT = "/Users/Manoj/calwbot/logs/navigator/repo_memory"
KINDS = ("decline", "failure", "audit")
TEXT_CHARS = 2000
MAX_FILES = 50
# B3 builder/verifier view caps: entries, compact-JSON chars in all, text chars, files per entry
CONTEXT_ENTRIES, CONTEXT_CHARS, CONTEXT_TEXT, CONTEXT_FILES = 8, 2500, 300, 6
CONTEXT_TITLE = 80
_log = get_logger("navigator.repo_memory")


def enabled(settings=None) -> bool:
    """The switch: a Settings value (even False) wins; without one the env decides. The env path
    is lenient ("maybe" is off) where Settings.from_env raises - the CLI must never crash on it."""
    value = getattr(settings, "repo_memory", None)
    if value is not None:
        return bool(value)
    return os.getenv("NAVIGATOR_REPO_MEMORY", "0").strip().lower() in ("1", "true")


def root_for(settings=None) -> str:
    """Directory of the per-repo logs: settings.repo_memory_root, else NAVIGATOR_REPO_MEMORY_ROOT,
    else DEFAULT_ROOT - read at call time, never baked into Settings."""
    return (getattr(settings, "repo_memory_root", None) or os.getenv("NAVIGATOR_REPO_MEMORY_ROOT")
            or DEFAULT_ROOT)


def path_for(repo, root) -> Path:
    return Path(root) / f"{Path(str(repo)).name}.jsonl"


def _file_list(affected_files, files) -> list[str]:
    """affected_files first (sorted), then the remaining touched files (sorted), MAX_FILES in all:
    a decline's own files are never dropped by the cap."""
    first = sorted({str(f) for f in (affected_files or ()) if f})
    rest = sorted({str(f) for f in (files or ()) if f} - set(first))
    return (first + rest)[:MAX_FILES]


def remember(settings, repo, *, kind, run_id, proposal_id, reason_code=None, text="", files=(),
             affected_files=(), source="", verified_sha=None, base_sha=None, title=None) -> bool:
    """Append one entry for `repo`. True when a line was written. False when the switch is off,
    `repo` is falsy (repo_memory.no_repo warning, only while on), `kind` is unknown or the write
    failed (repo_memory.write_failed warning with the repo basename, the kind and the exception
    class only). Never raises."""
    try:
        if not enabled(settings):
            return False
        if not repo:
            _log.warning("repo_memory.no_repo", kind=kind, run_id=run_id)
            return False
        if kind not in KINDS:
            raise ValueError(f"unknown repo_memory kind {kind!r}")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "repo": str(repo),
            "run_id": run_id,
            "proposal_id": proposal_id,
            "reason_code": reason_code,
            "text": str(text or "")[:TEXT_CHARS],
            "files": _file_list(affected_files, files),
            "source": source,
            "verified_sha": verified_sha,
            "base_sha": base_sha,
            "title": title,
        }
        line = json.dumps(entry, separators=(",", ":"), default=str)
        root = root_for(settings)
        os.makedirs(root, 0o700, exist_ok=True)
        fd = os.open(str(path_for(repo, root)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))   # one write: the line lands whole
        finally:
            os.close(fd)
        return True
    except Exception as e:
        _log.warning("repo_memory.write_failed", repo=Path(str(repo)).name if repo else None,
                     kind=kind, error=type(e).__name__)
        return False


def recent(repo, limit: int = 20, files=None, root=None) -> list[dict]:
    """The last `limit` entries for `repo`, newest first. With `files` (any iterable, even empty)
    only entries whose files intersect it are kept. A missing or unreadable file is [] and a
    corrupt line (crash mid-write) is skipped, as in OutcomeStore._read_all. `root` None resolves
    like root_for()."""
    path = path_for(repo, root or root_for())
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    wanted = {str(f) for f in files} if files is not None else None
    kept: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if wanted is not None:
            names = entry.get("files")
            if not isinstance(names, list) or not ({str(n) for n in names} & wanted):
                continue
        kept.append(entry)
    tail = kept[-limit:] if limit > 0 else []
    return list(reversed(tail))


def _context_files(files, affected) -> list[str]:
    """The entry's files for the view: those in `affected` first, then the rest, each in stored
    order, CONTEXT_FILES in all. A non-list is no files; items are coerced to str."""
    if not isinstance(files, (list, tuple)):
        return []
    names = [str(f) for f in files]
    wanted = {str(a) for a in (affected or ())}
    ordered = [n for n in names if n in wanted] + [n for n in names if n not in wanted]
    return ordered[:CONTEXT_FILES]


def compact(entries, affected=()) -> list[dict]:
    """Builder/verifier view of `entries` (as recent() hands them over: newest first), in order:
    {kind, reason_code, text[:300], files (affected first, <=6), ts, title[:80]} - no shas, source,
    run_id or repo. Stops at CONTEXT_ENTRIES entries or at the FIRST entry that would push the
    running compact-JSON total (sum of len(json.dumps(e, separators=(",", ":")))) past
    CONTEXT_CHARS; that entry and everything behind it are dropped (stop, never skip), so a single
    oversize newest entry gives []. text and files are coerced defensively; a corrupt entry is
    never a reason to raise."""
    out: list[dict] = []
    total = 0
    for entry in entries or ():
        if len(out) >= CONTEXT_ENTRIES:
            break
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        view = {
            "kind": entry.get("kind"),
            "reason_code": entry.get("reason_code"),
            "text": str(entry.get("text") if entry.get("text") is not None else "")[:CONTEXT_TEXT],
            "files": _context_files(entry.get("files"), affected),
            "ts": entry.get("ts"),
            "title": str(title)[:CONTEXT_TITLE] if title is not None else None,
        }
        size = len(json.dumps(view, separators=(",", ":"), default=str))
        if total + size > CONTEXT_CHARS:
            break
        out.append(view)
        total += size
    return out
