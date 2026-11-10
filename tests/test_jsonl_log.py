"""navigator/jsonl_log.py (housekeeping 2026-11-10): append one line to a JSON-lines log under a
shared flock on <path>.lock, and past a byte cap trim it to its newest complete lines under an
exclusive non-blocking flock (temp file + os.replace, mode 0600). Trimming never raises and never
loses the line being written."""
import fcntl
import json
import logging
import os
import stat
import threading

import pytest

from navigator import jsonl_log
from navigator.jsonl_log import append_line


def _lines(path):
    return path.read_bytes().decode("utf-8").splitlines()


def _fill(path, n):
    """n complete JSON lines of about 60 bytes each, written directly (no trimming), mode 0600."""
    with open(path, "wb") as fh:
        for i in range(n):
            fh.write((json.dumps({"i": i, "pad": "x" * 40}) + "\n").encode("utf-8"))
    os.chmod(path, 0o600)


def _no_tmp(tmp_path):
    return not list(tmp_path.glob("*.tmp"))


def _fds():
    return len(os.listdir("/dev/fd"))


def test_below_cap_appends_without_rewrite(tmp_path):
    # guard: below the cap a write is a plain append - the file is never rewritten
    path = tmp_path / "log.jsonl"
    append_line(str(path), '{"n": 1}')
    ino = path.stat().st_ino
    append_line(str(path), '{"n": 2}', max_bytes=1000)
    assert path.stat().st_ino == ino
    assert _lines(path) == ['{"n": 1}', '{"n": 2}']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _no_tmp(tmp_path)


def test_over_cap_keeps_newest_lines_mode_0600(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 30)                                   # ~1800 bytes: over a 1000-byte cap
    with open(path, "ab") as fh:
        fh.write(b'{"raw": "\xff\xfe"}\n')            # not UTF-8: the trim works on bytes, never decodes
    ino = path.stat().st_ino
    append_line(str(path), '{"newest": true}', max_bytes=1000, keep_lines=5)
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    kept = raw.split(b"\n")[:-1]
    assert len(kept) == 5                             # at most keep_lines (5 x ~60 bytes fit the budget)
    assert kept[-1] == b'{"newest": true}'            # the line being written is the newest kept
    assert kept[-2] == b'{"raw": "\xff\xfe"}'          # byte-for-byte
    assert [json.loads(k)["i"] for k in kept[:-2]] == [27, 28, 29]   # the newest, in order
    assert path.stat().st_ino != ino                  # rewritten (temp + replace)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _no_tmp(tmp_path)


def test_trim_respects_byte_budget_so_it_does_not_retrim(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 30)
    append_line(str(path), '{"n": "first"}', max_bytes=1000, keep_lines=1000)
    assert path.stat().st_size <= 500                 # max_bytes // 2, so the next writes are plain appends
    assert _lines(path)[-1] == '{"n": "first"}'
    assert len(_lines(path)) > 1                      # the budget, not just the newest line
    ino = path.stat().st_ino
    append_line(str(path), '{"n": "second"}', max_bytes=1000, keep_lines=1000)
    assert path.stat().st_ino == ino
    assert _lines(path)[-2:] == ['{"n": "first"}', '{"n": "second"}']


def test_appender_blocks_during_trim_and_lands_in_new_file(tmp_path):
    """No line lost: while a trim holds the exclusive lock an append waits, then opens the NEW file."""
    path = tmp_path / "log.jsonl"
    path.write_text('{"old": 1}\n')
    os.chmod(path, 0o600)
    errors = []

    def write():
        try:
            append_line(str(path), '{"late": 1}')
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    lk = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    t = threading.Thread(target=write, daemon=True)
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)                 # the test plays the trimmer
        t.start()
        t.join(0.5)
        assert t.is_alive(), "the append did not wait for the exclusive lock"
        assert "late" not in path.read_text()         # not in the old file before the replace
        new = tmp_path / "replacement"
        new.write_text('{"trimmed": 1}\n')
        os.replace(new, path)
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        os.close(lk)
    t.join(5)
    assert not t.is_alive() and errors == []
    assert _lines(path) == ['{"trimmed": 1}', '{"late": 1}']


def test_trim_skipped_while_lock_busy(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 30)
    ino = path.stat().st_ino
    before = _fds()
    lk = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lk, fcntl.LOCK_SH)                 # another writer is mid-append
        append_line(str(path), '{"n": "busy"}', max_bytes=1000, keep_lines=5)   # must not block
        assert path.stat().st_ino == ino              # trim skipped: the next write retries
        assert len(_lines(path)) == 31 and _lines(path)[-1] == '{"n": "busy"}'
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        os.close(lk)
    assert _fds() == before
    append_line(str(path), '{"n": "next"}', max_bytes=1000, keep_lines=5)
    assert path.stat().st_ino != ino
    assert len(_lines(path)) == 5 and _lines(path)[-2:] == ['{"n": "busy"}', '{"n": "next"}']
    assert _no_tmp(tmp_path)


def test_lock_unavailable_still_appends(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 30)
    os.mkdir(str(path) + ".lock")                     # the lock file cannot be opened
    ino = path.stat().st_ino
    before = _fds()
    append_line(str(path), '{"n": "nolock"}', max_bytes=1000, keep_lines=5)
    assert _fds() == before
    assert len(_lines(path)) == 31 and _lines(path)[-1] == '{"n": "nolock"}'
    assert path.stat().st_ino == ino                  # no lock, no trim
    assert _no_tmp(tmp_path)


def test_trim_failure_never_raises(tmp_path, monkeypatch, caplog):
    path = tmp_path / "log.jsonl"
    _fill(path, 30)

    def boom(*a, **k):
        raise OSError(f"disk says no {path}")

    monkeypatch.setattr(jsonl_log.os, "replace", boom)
    caplog.set_level(logging.WARNING, logger="navigator.jsonl_log")
    before = _fds()
    append_line(str(path), '{"n": "kept"}', max_bytes=1000, keep_lines=5)   # must NOT raise
    assert _fds() == before                           # the lock fd is closed on the failure path too
    assert len(_lines(path)) == 31 and _lines(path)[-1] == '{"n": "kept"}'
    assert _no_tmp(tmp_path)                          # the temp is unlinked
    recs = [r for r in caplog.records if r.name == "navigator.jsonl_log"]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    assert "OSError" in msg                           # the exception class only
    assert "disk says no" not in msg and str(tmp_path) not in msg
    # an APPEND failure propagates (both callers catch and warn) and leaks no fd
    clash = tmp_path / "afile"
    clash.write_text("x")
    with pytest.raises(OSError):
        append_line(str(clash / "x.jsonl"), "{}")
    assert _fds() == before


def test_threads_lines_stay_whole(tmp_path):
    # guard: 4 threads x 200 lines under a tiny cap - every line whole, each thread's order kept
    path = tmp_path / "log.jsonl"

    def work(t):
        for i in range(200):
            append_line(str(path), json.dumps({"t": t, "i": i, "pad": "y" * 30}),
                        max_bytes=4000, keep_lines=40)

    ts = [threading.Thread(target=work, args=(t,)) for t in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = _lines(path)
    assert lines and path.stat().st_size <= 4000
    last: dict = {}
    for line in lines:
        e = json.loads(line)                          # whole: no torn or merged line
        assert e["i"] > last.get(e["t"], -1)
        last[e["t"]] = e["i"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _no_tmp(tmp_path)
