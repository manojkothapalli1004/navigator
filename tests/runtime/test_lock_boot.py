"""U7: the single-flight lock records the boot time beside the PID; a lock written in an
earlier boot is stale even when its PID now belongs to an unrelated live process."""
import os
import types

import pytest

from navigator.runtime import lock as lock_mod
from navigator.runtime.lock import SingleFlightLock


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(lock_mod, "_BOOT_TIME", None)


def test_lock_writes_pid_and_boot(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 1000)
    p = tmp_path / "nav.lock"
    with SingleFlightLock(p).acquire():
        assert p.read_text() == f"{os.getpid()} 1000"
    assert not p.exists()


def test_lock_writes_pid_only_without_boot(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: None)
    p = tmp_path / "nav.lock"
    with SingleFlightLock(p).acquire():
        assert p.read_text() == str(os.getpid())


def test_stale_when_boot_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text(f"{os.getpid()} 1")  # live PID, but written in another boot
    with SingleFlightLock(p).acquire():
        assert p.read_text() == f"{os.getpid()} 2"
    assert not p.exists()


def test_held_when_same_boot_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text(f"{os.getpid()} 2")
    with pytest.raises(RuntimeError, match="single-flight lock held"):
        with SingleFlightLock(p).acquire():
            pass
    assert p.read_text() == f"{os.getpid()} 2"  # a live holder's lock is left alone


def test_legacy_pid_only_live_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text(str(os.getpid()))
    with pytest.raises(RuntimeError, match="single-flight lock held"):
        with SingleFlightLock(p).acquire():
            pass
    assert p.exists()


def test_legacy_pid_only_dead_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text("999999999")  # PID far above pid_max: guaranteed dead
    with SingleFlightLock(p).acquire():
        pass
    assert not p.exists()


def test_unknown_boot_keeps_pid_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: None)
    p = tmp_path / "nav.lock"
    p.write_text(f"{os.getpid()} 1")
    with pytest.raises(RuntimeError, match="single-flight lock held"):
        with SingleFlightLock(p).acquire():
            pass
    assert p.exists()


def test_garbage_second_field(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text(f"{os.getpid()} abc")
    with pytest.raises(RuntimeError, match="single-flight lock held"):
        with SingleFlightLock(p).acquire():
            pass
    assert p.exists()


def test_is_held_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "boot_time", lambda: 2)
    p = tmp_path / "nav.lock"
    p.write_text(f"{os.getpid()} 1")
    assert SingleFlightLock(p).is_held() is False
    p.write_text(f"{os.getpid()} 2")
    assert SingleFlightLock(p).is_held() is True


def test_boot_time_parses_sysctl(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return types.SimpleNamespace(
            returncode=0,
            stdout="{ sec = 1793236549, usec = 770377 } Wed Oct 28 21:15:49 2026\n",
            stderr="")
    monkeypatch.setattr(lock_mod, "_BOOT_TIME", None)
    monkeypatch.setattr(lock_mod.subprocess, "run", fake_run)
    assert lock_mod.boot_time() == 1793236549
    assert seen["argv"] == ["sysctl", "-n", "kern.boottime"]
    assert seen["timeout"] == 5
    assert lock_mod._BOOT_TIME == 1793236549  # cached after the first success
    # cached: a now-failing sysctl is not consulted
    monkeypatch.setattr(lock_mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert lock_mod.boot_time() == 1793236549


def test_boot_time_read_before_create(tmp_path, monkeypatch):
    p = tmp_path / "nav.lock"
    seen = {}

    def recording_boot_time():
        seen["existed"] = p.exists()
        return 7
    monkeypatch.setattr(lock_mod, "boot_time", recording_boot_time)
    with SingleFlightLock(p).acquire():
        assert p.read_text() == f"{os.getpid()} 7"
    assert seen["existed"] is False  # boot time read before the O_EXCL create


def test_boot_time_none_on_failure(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("sysctl")
    monkeypatch.setattr(lock_mod, "_BOOT_TIME", None)
    monkeypatch.setattr(lock_mod.subprocess, "run", missing)
    assert lock_mod.boot_time() is None
    assert lock_mod._BOOT_TIME is None  # a failure is never cached

    monkeypatch.setattr(lock_mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="unknown oid"))
    assert lock_mod.boot_time() is None
    assert lock_mod._BOOT_TIME is None

    monkeypatch.setattr(lock_mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="garbage", stderr=""))
    assert lock_mod.boot_time() is None

    def timeout(*a, **k):
        raise lock_mod.subprocess.TimeoutExpired(cmd=a[0], timeout=5)
    monkeypatch.setattr(lock_mod.subprocess, "run", timeout)
    assert lock_mod.boot_time() is None
