"""U7b: boot_time() runs /usr/sbin/sysctl by absolute path when that file exists, so the
reboot-safe lock works in launchd services whose PATH has no /usr/sbin; otherwise it falls
back to a bare "sysctl" looked up on PATH."""
import os
import types

import pytest

from navigator.runtime import lock as lock_mod


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(lock_mod, "_BOOT_TIME", None)


def _recording_run(seen):
    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return types.SimpleNamespace(returncode=0, stdout="{ sec = 42, usec = 0 }", stderr="")
    return fake_run


def test_uses_absolute_sysctl_when_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(lock_mod.subprocess, "run", _recording_run(seen))
    monkeypatch.setattr(lock_mod.os.path, "exists", lambda p: p == "/usr/sbin/sysctl")
    assert lock_mod.boot_time() == 42
    assert seen["argv"][0] == "/usr/sbin/sysctl"
    assert seen["argv"][1:] == ["-n", "kern.boottime"]
    assert seen["timeout"] == 5


def test_falls_back_to_bare_sysctl(monkeypatch):
    seen = {}
    monkeypatch.setattr(lock_mod.subprocess, "run", _recording_run(seen))
    monkeypatch.setattr(lock_mod.os.path, "exists", lambda p: False)
    assert lock_mod.boot_time() == 42
    assert seen["argv"][0] == "sysctl"
    assert seen["argv"][1:] == ["-n", "kern.boottime"]
    assert seen["timeout"] == 5


def test_works_without_usr_sbin_on_path(monkeypatch):
    if not os.path.exists("/usr/sbin/sysctl"):
        assert True  # not a macOS-style host: the absolute-path branch cannot be exercised live
        return
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # the launchd PATH that lacks /usr/sbin
    monkeypatch.setattr(lock_mod, "_BOOT_TIME", None)
    b = lock_mod.boot_time()
    assert isinstance(b, int) and b > 0


def test_absolute_path_failure_is_still_none_and_uncached(monkeypatch):
    monkeypatch.setattr(lock_mod.os.path, "exists", lambda p: p == "/usr/sbin/sysctl")
    monkeypatch.setattr(lock_mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="unknown oid"))
    assert lock_mod.boot_time() is None
    assert lock_mod._BOOT_TIME is None
