"""Conductor tests must never start launchctl or kill anything real (the restart feature builds
exactly such a command). Every way a test process could spawn one is wrapped: an argv (or shell
text) that mentions launchctl raises instead of running; everything else passes through."""
import os
import subprocess

import pytest

LAUNCHCTL_ATTEMPTS: list = []


class LaunchctlBlocked(RuntimeError):
    pass


def _mentions_launchctl(obj) -> bool:
    if isinstance(obj, (str, bytes, os.PathLike)):
        return "launchctl" in os.fsdecode(obj)
    if isinstance(obj, (list, tuple)):
        return any(_mentions_launchctl(x) for x in obj)
    return False


def _check(*args):
    if _mentions_launchctl(args):
        LAUNCHCTL_ATTEMPTS.append(args)
        raise LaunchctlBlocked("a test tried to run launchctl")


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch):
    LAUNCHCTL_ATTEMPTS.clear()
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            _check(args)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execvp", "execl", "execlp"):
        real = getattr(os, name, None)
        if real is None:
            continue
        monkeypatch.setattr(os, name, (lambda real: lambda *a, **k: (_check(*a), real(*a, **k))[1])(real))
    for name in ("spawnv", "spawnvp", "spawnl", "spawnlp"):
        real = getattr(os, name, None)
        if real is not None:
            monkeypatch.setattr(os, name, (lambda real: lambda *a, **k: (_check(*a), real(*a, **k))[1])(real))
    yield
