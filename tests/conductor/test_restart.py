import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from navigator.conductor import restart
from tests.conductor.conftest import LAUNCHCTL_ATTEMPTS, LaunchctlBlocked

WORKTREE = str(Path(__file__).resolve().parents[2])


class FakeSpawn:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return object()


def test_canary_launchctl_never_runs():
    with pytest.raises(LaunchctlBlocked):
        subprocess.Popen(["/bin/launchctl", "list"])
    with pytest.raises(LaunchctlBlocked):
        subprocess.Popen(["/bin/sh", "-c", "sleep 1; exec /bin/launchctl kickstart -k gui/1/x"])
    with pytest.raises(LaunchctlBlocked):
        os.system("launchctl list")
    assert len(LAUNCHCTL_ATTEMPTS) == 3


def test_schedule_with_default_spawn_hits_the_canary_not_launchctl(tmp_path):
    with pytest.raises(LaunchctlBlocked):
        restart.schedule("conductor", 10, tmp_path, clock=lambda: 1000.0)
    assert not (tmp_path / "restart.json").exists()  # cleaned up


def test_command_exact_and_allowlist():
    assert restart.command("conductor", 10, 501) == [
        "/bin/sh", "-c", "sleep 10; exec /bin/launchctl kickstart -k gui/501/com.calwbot.conductor"]
    assert restart.command("navigator", 5, 501)[2].endswith("gui/501/com.calwbot.navigator")
    for bad in ("conductor; rm -rf /", "com.calwbot.conductor", "", None, "x"):
        with pytest.raises(Exception):
            restart.command(bad, 10, 501)
    assert restart.LABELS == {"conductor": "com.calwbot.conductor", "navigator": "com.calwbot.navigator"}


@pytest.mark.parametrize("raw,want", [(0, 5), (-5, 5), (True, 10), (False, 10), (float("nan"), 10),
                                      (float("inf"), 10), (9999, 60), (7, 7), (7.9, 7), ("7", 10),
                                      (None, 10), ("1; rm", 10)])
def test_clamp_delay(raw, want):
    assert restart.clamp_delay(raw) == want


def test_precheck_lists_active_only():
    class H:
        def snapshot(self):
            return [{"id": "h1", "state": "running", "title": "a", "age": 12.0},
                    {"id": "h2", "state": "queued", "title": "b", "age": 3.0},
                    {"id": "h3", "state": "done", "title": "c", "age": 99.0}]
    assert [h["id"] for h in restart.precheck(H())] == ["h1", "h2"]
    assert restart.precheck(None) == []


def test_schedule_spawns_detached_and_writes_marker(tmp_path):
    sp = FakeSpawn()
    out = restart.schedule("conductor", 10, tmp_path, spawn=sp, clock=lambda: 1000.0, pid=lambda: 4242,
                           helpers=[{"id": "h1", "title": "t", "age": 1}], card_code="ABC234", via="web")
    assert out["ok"] is True and out["old_pid"] == 4242 and out["delay_s"] == 10
    argv, kw = sp.calls[0]
    assert argv[:2] == ["/bin/sh", "-c"] and "gui/" in argv[2]
    assert kw["start_new_session"] is True and kw["close_fds"] is True
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    p = tmp_path / "restart.json"
    assert (p.stat().st_mode & 0o777) == 0o600
    m = json.loads(p.read_text())
    assert m["label"] == "conductor" and m["old_pid"] == 4242 and m["scheduled_at"] == 1000.0
    assert m["card_code"] == "ABC234" and m["via"] == "web" and m["helpers_lost"][0]["id"] == "h1"
    # second call inside 120 s: refused, nothing spawned
    out2 = restart.schedule("conductor", 10, tmp_path, spawn=sp, clock=lambda: 1119.0)
    assert out2["ok"] is False and len(sp.calls) == 1
    # after 120 s it is allowed again
    assert restart.schedule("conductor", 10, tmp_path, spawn=sp, clock=lambda: 1121.0)["ok"] is True
    assert len(sp.calls) == 2


def test_schedule_oserror_removes_marker(tmp_path):
    out = restart.schedule("navigator", 10, tmp_path, spawn=FakeSpawn(OSError("no")), clock=lambda: 5.0)
    assert out["ok"] is False and not (tmp_path / "restart.json").exists()


def test_schedule_refuses_bad_label_and_disabled(tmp_path, monkeypatch):
    sp = FakeSpawn()
    assert restart.schedule("zzz", 10, tmp_path, spawn=sp, clock=lambda: 1.0)["ok"] is False
    monkeypatch.setenv("CONDUCTOR_RESTART_TOOL", "0")
    assert restart.schedule("conductor", 10, tmp_path, spawn=sp, clock=lambda: 1.0)["ok"] is False
    assert sp.calls == []


def test_real_detach_survives_group_kill(tmp_path):
    """Simulates launchd's group kill: a stand-in 'helper' in its own group schedules a detached child,
    then its whole group is SIGKILLed; the child (own session) must still finish. No launchctl."""
    ran = tmp_path / "ran"
    child_pid = tmp_path / "child.pid"
    prog = (
        "import subprocess, sys, time\n"
        "from navigator.conductor import restart\n"
        f"script = 'echo $$ > {child_pid}; sleep 1; touch {ran}'\n"
        "def sp(argv, **kw):\n"
        "    return subprocess.Popen(['/bin/sh', '-c', script], **kw)\n"
        f"restart.schedule('conductor', 5, __import__('pathlib').Path({str(tmp_path)!r}), spawn=sp)\n"
        "time.sleep(60)\n")
    env = {**os.environ, "PYTHONPATH": WORKTREE}
    helper = subprocess.Popen([sys.executable, "-c", prog], env=env, start_new_session=True)
    try:
        deadline = time.time() + 15
        while not child_pid.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert child_pid.exists()
        time.sleep(0.1)
        cpid = int(child_pid.read_text())
        assert os.getsid(cpid) != os.getsid(0) and os.getsid(cpid) != os.getsid(helper.pid)
        assert os.getpgid(cpid) != os.getpgrp() and os.getpgid(cpid) != helper.pid
        os.killpg(helper.pid, signal.SIGKILL)
        helper.wait(timeout=5)
        deadline = time.time() + 10
        while not ran.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert ran.exists()
    finally:
        try:
            os.killpg(helper.pid, signal.SIGKILL)
        except OSError:
            pass


def _marker(tmp_path, **kw):
    m = {"label": "conductor", "scheduled_at": 1000.0, "delay_s": 10, "old_pid": 111, "card_code": "X",
         "via": "web", "helpers_lost": []}
    m.update(kw)
    (tmp_path / "restart.json").write_text(json.dumps(m))


def test_announce_marker_and_twice(tmp_path):
    _marker(tmp_path, helpers_lost=[{"id": "helper-A", "title": "T" * 200}])
    msg = restart.announce(tmp_path, clock=lambda: 1030.0, pid=lambda: 222)
    assert msg.startswith("restarted at ") and "(pid 222)" in msg and "was pid 111" in msg
    assert "1 helper(s) lost" in msg and "helper-A" in msg and "T" * 81 not in msg
    assert not (tmp_path / "restart.json").exists()
    assert restart.announce(tmp_path, clock=lambda: 1031.0, pid=lambda: 222) is None


def test_announce_no_marker_none(tmp_path):
    assert restart.announce(tmp_path, clock=lambda: 1.0, pid=lambda: 1) is None


def test_announce_without_helpers_has_no_lost_clause(tmp_path):
    _marker(tmp_path)
    msg = restart.announce(tmp_path, clock=lambda: 1030.0, pid=lambda: 2)
    assert "lost" not in msg and "was pid 111" in msg


def test_announce_leaves_marker_inside_delay_window(tmp_path):
    _marker(tmp_path, scheduled_at=2000.0)
    assert restart.announce(tmp_path, clock=lambda: 2000.0, pid=lambda: 2) is None
    assert (tmp_path / "restart.json").exists()
    # and the 120 s guard still blocks a new schedule
    assert restart.schedule("conductor", 10, tmp_path, spawn=FakeSpawn(), clock=lambda: 2001.0)["ok"] is False


def test_announce_stale_marker(tmp_path):
    _marker(tmp_path, scheduled_at=1000.0)
    msg = restart.announce(tmp_path, clock=lambda: 1000.0 + 700, pid=lambda: 9)
    assert "stale" in msg and msg.startswith("restarted at ")
    assert not (tmp_path / "restart.json").exists()


def test_announce_corrupt_marker(tmp_path):
    (tmp_path / "restart.json").write_text("{not json")
    msg = restart.announce(tmp_path, clock=lambda: 1.0, pid=lambda: 9)
    assert msg.startswith("restarted at ") and not (tmp_path / "restart.json").exists()
