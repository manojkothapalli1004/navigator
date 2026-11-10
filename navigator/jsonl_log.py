"""Append-only JSON-lines logs with a size cap (housekeeping 2026-11-10; stdlib only).

append_line() is the one writer of credits_events.jsonl (credits._event) and of the per-repo
memory logs (runtime/repo_memory.remember); both used to grow forever.

Locking is on <path>.lock, never on the log itself (a trim replaces the log):
- every append holds a SHARED flock from opening the log to its single os.write(), so writers
  never wait on each other and O_APPEND keeps each line whole;
- a writer that finds the file over max_bytes after its own line is written unlocks and retries
  the lock EXCLUSIVE and NON-BLOCKING: busy (an append or another trim in flight) -> it skips and a
  later write retries. Under the exclusive lock no writer is between open() and write(), so none
  can still hold the old file while the trim reads it and replaces it: no line is lost, the one
  just written included.

The trim works on BYTES and never decodes: it splits the file on b"\\n", drops only a trailing
fragment without a newline (a crash mid-write) and keeps the newest complete lines - at most
keep_lines, at most max_bytes // 2 bytes in all (so the next writes are plain appends again, not
a re-trim each), and always the newest line. It writes a temp file next to the log (O_EXCL,
0600) and os.replace()s it over the log; the temp is unlinked on failure.

Errors: an append failure propagates (both callers catch and warn). A trim failure never raises:
one warning naming the exception class only (no path, no message). If the lock cannot be opened or
taken the line is appended without it and no trim is attempted.

Accepted risks: the no-lock fallback, and processes started before this was deployed, append
without the lock, so a line they write during another process's trim can be lost; a trim that
keeps failing re-reads the file and warns on every later write.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import uuid

log = logging.getLogger("navigator.jsonl_log")

MAX_BYTES = 2 * 1024 * 1024
KEEP_LINES = 2000


def append_line(path, line: str, *, max_bytes: int | None = None, keep_lines: int | None = None) -> None:
    """Append `line` and a newline to `path` (created 0600) in one write, then trim the file when it
    is over the cap. max_bytes / keep_lines None -> MAX_BYTES / KEEP_LINES, read at call time."""
    path = os.fspath(path)
    cap = MAX_BYTES if max_bytes is None else max_bytes
    keep = KEEP_LINES if keep_lines is None else keep_lines
    lk = None
    try:
        try:
            lk = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lk, fcntl.LOCK_SH)
            locked = True
        except OSError:
            locked = False                       # append without the lock; never trim
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))   # one write: the line lands whole
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if not locked or size <= cap:
            return
        try:
            fcntl.flock(lk, fcntl.LOCK_UN)
            _trim(path, lk, cap, keep)
        except Exception as e:  # noqa: BLE001 - a trim must never break the write
            log.warning("jsonl_log.trim_failed error=%s", type(e).__name__)
    finally:
        if lk is not None:
            os.close(lk)                         # also releases any flock still held


def _trim(path: str, lk: int, cap: int, keep: int) -> None:
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return                                   # busy: the next write over the cap retries
    if os.stat(path).st_size <= cap:
        return                                   # another writer trimmed it first
    with open(path, "rb") as fh:
        raw = fh.read()
    body = b"".join(_newest(raw.split(b"\n")[:-1], keep, cap // 2))   # [:-1]: b"" or a crash fragment
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _newest(lines: list[bytes], keep: int, budget: int) -> list[bytes]:
    """The newest complete lines, each with its newline, oldest first: at most `keep`, at most
    `budget` bytes in all, and always the newest one."""
    out: list[bytes] = []
    total = 0
    for ln in reversed(lines):
        n = len(ln) + 1
        if out and (len(out) >= keep or total + n > budget):
            break
        out.append(ln + b"\n")
        total += n
    out.reverse()
    return out
