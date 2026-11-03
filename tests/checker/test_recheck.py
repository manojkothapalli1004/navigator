import subprocess
import threading
from pathlib import Path

import pytest

from navigator.checker import preview, recheck
from navigator.runtime.executor import suite_green
from navigator.safety.command_runner import FRONTEND_SUITE


def test_verify_source_copies_worktree_not_branch(tmp_path, monkeypatch):
    src = tmp_path / "wt" / "frontend"
    (src / "app").mkdir(parents=True); (src / "node_modules").mkdir(); (src / ".next").mkdir()
    (src / "app" / "page.tsx").write_text("uncommitted\n")
    live = tmp_path / "live"; (live / "frontend" / "node_modules").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(preview, "ACT_REPO", live)
    monkeypatch.setattr(preview, "stop", lambda port=3001: None)
    monkeypatch.setattr(preview, "_start_and_wait", lambda fe, port: calls.append(fe) or "http://x")
    monkeypatch.setattr(preview.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    work = tmp_path / "work"
    preview.build(src, work)
    fe = work / "frontend"
    assert (fe / "app" / "page.tsx").read_text() == "uncommitted\n"
    assert not (fe / ".next").exists()
    assert not any(isinstance(c, list) and c[:2] == ["git", "archive"] for c in calls)
    assert any(isinstance(c, list) and c[:2] == ["cp", "-cR"] for c in calls)  # node_modules from live


def test_preview_lock_is_exclusive(tmp_path):
    lock = tmp_path / "preview.lock"
    with preview.lock(lock, timeout_s=1):
        out = {}
        def other():
            try:
                with preview.lock(lock, timeout_s=0.2):
                    out["got"] = True
            except preview.PreviewBusy:
                out["busy"] = True
        t = threading.Thread(target=other); t.start(); t.join()
        assert out == {"busy": True}
    with preview.lock(lock, timeout_s=0.2):
        pass


def _slow_checker(tmp_path, monkeypatch, stopped):
    py = tmp_path / "python"; py.write_text("#!/bin/sh\nsleep 5\n"); py.chmod(0o755)
    monkeypatch.setattr(recheck, "CHECKER_PYTHON", py)
    monkeypatch.setattr(preview, "stop", lambda port=preview.PREVIEW_PORT: stopped.append(port))  # never a real kill
    monkeypatch.setattr(preview, "LOCK_PATH", tmp_path / "preview.lock")


def test_recheck_timeout_is_126(tmp_path, monkeypatch):
    stopped = []
    _slow_checker(tmp_path, monkeypatch, stopped)
    rc, out = recheck.run_recheck(tmp_path, ["abc"], timeout_s=0.3)
    assert rc == 126 and "timed out" in out


def test_timeout_stops_the_orphaned_preview(tmp_path, monkeypatch):
    # 2026-11-02: the killed checker left its `next start` on :3001
    stopped = []
    _slow_checker(tmp_path, monkeypatch, stopped)
    rc, out = recheck.run_recheck(tmp_path, ["abc"], timeout_s=0.3)
    assert rc == 126 and stopped == [3001] and "stopped the leftover preview" in out


def test_timeout_leaves_another_runs_preview_alone(tmp_path, monkeypatch):
    # timed out while ANOTHER checker run holds the preview lock: its :3001 is legitimately in use
    import fcntl
    stopped = []
    _slow_checker(tmp_path, monkeypatch, stopped)
    held = open(tmp_path / "preview.lock", "w"); fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc, out = recheck.run_recheck(tmp_path, ["abc"], timeout_s=0.3)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN); held.close()
    assert rc == 126 and stopped == []


def test_no_timeout_no_stop(tmp_path, monkeypatch):
    stopped = []
    _slow_checker(tmp_path, monkeypatch, stopped)
    fast = tmp_path / "fast"; fast.write_text("#!/bin/sh\nexit 0\n"); fast.chmod(0o755)
    monkeypatch.setattr(recheck, "CHECKER_PYTHON", fast)
    rc, _ = recheck.run_recheck(tmp_path, ["abc"], timeout_s=5)
    assert rc == 0 and stopped == []


def test_cleanup_never_targets_the_live_console(tmp_path, monkeypatch):
    stopped = []
    _slow_checker(tmp_path, monkeypatch, stopped)
    recheck.run_recheck(tmp_path, ["abc"], timeout_s=0.3)
    assert 3000 not in stopped


def test_recheck_missing_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(recheck, "CHECKER_PYTHON", tmp_path / "nope")
    rc, out = recheck.run_recheck(tmp_path, ["abc"])
    assert rc == 1 and "checker venv" in out


def test_recheck_argv_and_sealed_env(tmp_path, monkeypatch):
    py = tmp_path / "python"
    py.write_text('#!/bin/sh\necho "$@"\necho "proxy=$HTTPS_PROXY"\n'); py.chmod(0o755)
    monkeypatch.setattr(recheck, "CHECKER_PYTHON", py)
    rc, out = recheck.run_recheck(tmp_path / "fe", ["f1", "f2"])
    assert rc == 0
    assert f"-m navigator.checker verify --source {tmp_path / 'fe'} --fingerprint f1 --fingerprint f2" in out
    assert "proxy=http://127.0.0.1:9" in out


FRONT = ["frontend/components/X.tsx"]


def _suite():
    return [{"argv_list": list(a), "returncode": 0, "suite": "frontend"} for a in FRONTEND_SUITE]


def test_suite_green_checker_origin_requires_recheck():
    ok = {"argv_list": ["checker-verify", "f1"], "returncode": 0, "suite": "checker"}
    assert suite_green(_suite(), FRONT, frontend_dir="frontend", origin="site-checker") is False
    assert suite_green(_suite() + [ok], FRONT, frontend_dir="frontend", origin="site-checker") is True
    assert suite_green(_suite() + [dict(ok, returncode=1)], FRONT, frontend_dir="frontend",
                       origin="site-checker") is False
    untagged = {"argv_list": ["checker-verify", "f1"], "returncode": 0}
    assert suite_green(_suite() + [untagged], FRONT, frontend_dir="frontend", origin="site-checker") is False


def test_verify_preview_crash_is_rc2_with_build_output(tmp_path, monkeypatch, capsys):
    from navigator.checker import __main__ as cli
    from navigator.checker.checks import Finding
    from navigator.checker.store import FindingStore
    f = Finding("/", "phone", "tap-size", "x", "small")
    FindingStore(tmp_path / "store.json").record([f], "2026-11-01T00:00:00+00:00")
    def boom(source, work):
        raise subprocess.CalledProcessError(1, ["next", "build"], output="", stderr="Failed to fetch `Onest`")
    monkeypatch.setattr(cli.preview, "build", boom)
    monkeypatch.setattr(cli.preview, "stop", lambda port=3001: None)
    monkeypatch.setattr(cli.preview, "LOCK_PATH", tmp_path / "preview.lock")
    rc = cli.cmd_verify(["--source", str(tmp_path), "--fingerprint", f.fingerprint], root=tmp_path)
    assert rc == 2
    assert "Failed to fetch `Onest`" in capsys.readouterr().err
