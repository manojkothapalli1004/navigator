"""S3b: verifier subprocesses run without ACT/ORC URLs or tokens and behind dead
proxies. Builder-written test code is arbitrary Python executed by the verifier on
the host that runs ACT; it must not inherit the credentials or discovery the
Navigator process itself holds. Defence in depth, not a sandbox.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from navigator.contracts.enums import Role
from navigator.runtime.drivers.base import DriverRequest, DriverResult
from navigator.runtime.phases import (_DEAD_PROXY, _run_sealed, run_build_and_verify,
                                      sealed_verifier_env)
from navigator.runtime.worktree import WorktreeHandle
from navigator.safety.errors import CommandRejected

SECRETS = {
    "NAVIGATOR_ACT_BASE_URL": "http://127.0.0.1:8090",
    "NAVIGATOR_ORC_BASE_URL": "http://127.0.0.1:8091",
    "NAVIGATOR_REPO": "/some/repo",
    "NAVIGATOR_TELEGRAM_BOT_TOKEN": "tg-secret-token",
    "ACT_API_KEY": "act-secret",
    "ORC_API_KEY": "orc-secret",
    "SOME_SERVICE_TOKEN": "svc-secret",
    "lowercase_token": "lower-secret",
}
SECRET_VALUES = tuple(SECRETS.values())


@pytest.fixture
def leaky_env(monkeypatch):
    """Parent environment as the daemon would have it: URLs, tokens, a corporate
    proxy and a NO_PROXY that would bypass it for localhost."""
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HTTP_PROXY", "http://corp-proxy:3128")
    monkeypatch.setenv("https_proxy", "http://corp-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("KEEP_ME", "kept")
    return dict(SECRETS)


def _assert_sealed(env: dict):
    for key in SECRETS:
        assert key not in env, key
    for value in SECRET_VALUES:
        assert value not in env.values(), value
    assert not any(k.upper().startswith("NAVIGATOR_") for k in env)
    assert not any("ACT_" in k.upper() or "ORC_" in k.upper() or "TOKEN" in k.upper()
                   for k in env)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == _DEAD_PROXY, key
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""
    assert env["PATH"] == os.environ["PATH"]


# ---- the env itself -----------------------------------------------------------

def test_sealed_env_drops_secrets_sets_dead_proxies_keeps_the_rest(leaky_env):
    env = sealed_verifier_env()
    _assert_sealed(env)
    assert _DEAD_PROXY == "http://127.0.0.1:9"
    assert env["KEEP_ME"] == "kept"
    assert env.get("HOME") == os.environ.get("HOME")
    # the parent process is untouched
    assert os.environ["NAVIGATOR_ACT_BASE_URL"] == "http://127.0.0.1:8090"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"


def test_sealed_env_from_explicit_base_is_pure():
    base = {"PATH": "/usr/bin", "HOME": "/h", "NAVIGATOR_X": "1", "MY_ACT_URL": "u",
            "ORC_THING": "o", "GH_TOKEN": "t", "EDITOR": "vi"}
    env = sealed_verifier_env(base)
    assert set(env) == {"PATH", "HOME", "EDITOR", "HTTP_PROXY", "HTTPS_PROXY",
                        "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"}
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/h" and env["EDITOR"] == "vi"
    assert base["NAVIGATOR_X"] == "1"  # input not mutated


# ---- the spawn site ------------------------------------------------------------

class _InheritingRunner:
    """A runner whose run() takes no env and would spawn with the inherited one:
    records os.environ as seen at spawn time."""
    allowlist = ["python3"]

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def run(self, argv, cwd=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(os.environ)})
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class _EnvAwareRunner:
    """A runner that spawns with an explicit env kwarg."""
    allowlist = ["python3"]

    def __init__(self):
        self.calls = []

    def run(self, argv, cwd=None, env=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": env,
                           "environ": dict(os.environ)})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def test_run_sealed_replaces_environ_for_inheriting_runner_and_restores_it(leaky_env):
    runner = _InheritingRunner()
    before = dict(os.environ)
    r = _run_sealed(runner, ["python3", "-c", "pass"], Path("/tmp"))
    assert r.returncode == 0
    assert runner.calls[0]["cwd"] == Path("/tmp")
    _assert_sealed(runner.calls[0]["env"])
    assert dict(os.environ) == before


def test_run_sealed_passes_env_kwarg_to_env_aware_runner(leaky_env):
    runner = _EnvAwareRunner()
    before = dict(os.environ)
    _run_sealed(runner, ["python3", "-c", "pass"], Path("/tmp"))
    call = runner.calls[0]
    assert call["env"] is not None
    _assert_sealed(call["env"])
    _assert_sealed(call["environ"])
    assert dict(os.environ) == before


def test_run_sealed_restores_environ_when_runner_raises(leaky_env):
    runner = _InheritingRunner(fail_with=CommandRejected("nope"))
    before = dict(os.environ)
    with pytest.raises(CommandRejected):
        _run_sealed(runner, ["rm", "-rf", "/"], Path("/tmp"))
    _assert_sealed(runner.calls[0]["env"])
    assert dict(os.environ) == before


# ---- through run_build_and_verify ---------------------------------------------

class _Driver:
    def __init__(self, commands):
        self.commands = commands

    def run_role(self, request: DriverRequest) -> DriverResult:
        if request.role == Role.builder:
            return DriverResult(role=Role.builder,
                                file_writes=[("NOTES.txt", "sealed verifier\n")],
                                summary="wrote 1 file")
        return DriverResult(role=Role.verifier, commands=self.commands, summary="verify")


def _git_worktree(tmp_path) -> WorktreeHandle:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "--allow-empty", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return WorktreeHandle(repo=repo, path=repo, branch="nav/run-seal")


def test_verifier_commands_run_sealed_through_the_phase(tmp_path, leaky_env):
    wt = _git_worktree(tmp_path)
    runner = _InheritingRunner()
    cmds = [["python3", "-c", "print(1)"], ["python3", "-c", "print(2)"]]
    before = dict(os.environ)
    res = run_build_and_verify(_Driver(cmds), wt, run_id="run-seal", runner=runner,
                               proposal={"affected_files": ["NOTES.txt"]})
    assert res.verify_returncode == 0
    assert [c["argv"] for c in runner.calls] == cmds
    for call in runner.calls:
        _assert_sealed(call["env"])
    assert dict(os.environ) == before


def test_verifier_commands_run_sealed_with_env_aware_runner_through_the_phase(tmp_path, leaky_env):
    wt = _git_worktree(tmp_path)
    runner = _EnvAwareRunner()
    res = run_build_and_verify(_Driver([["python3", "-c", "print(1)"]]), wt,
                               run_id="run-seal2", runner=runner,
                               proposal={"affected_files": ["NOTES.txt"]})
    assert res.verify_returncode == 0
    assert len(runner.calls) == 1
    _assert_sealed(runner.calls[0]["env"])


# ---- a real subprocess ---------------------------------------------------------

def test_real_child_process_sees_the_sealed_environment(leaky_env):
    r = subprocess.run([sys.executable, "-c",
                        "import json, os; print(json.dumps(dict(os.environ)))"],
                       env=sealed_verifier_env(), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    child = json.loads(r.stdout)
    _assert_sealed(child)
    assert child["KEEP_ME"] == "kept"
    for value in SECRET_VALUES:
        assert value not in r.stdout


_PROBE = """
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8090/api/v1/agent/runs", timeout=5) as resp:
        print("REACHED", resp.status)
        sys.exit(1)
except Exception as e:
    print("BLOCKED", type(e).__name__, e)
    sys.exit(0)
"""


def test_real_child_cannot_reach_act_port_from_sealed_env(leaky_env):
    env = sealed_verifier_env()
    assert "NAVIGATOR_ACT_BASE_URL" not in env
    r = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("BLOCKED"), r.stdout
    assert "REACHED" not in r.stdout
