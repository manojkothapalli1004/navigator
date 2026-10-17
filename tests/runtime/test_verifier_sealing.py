"""S3b/S3c: verifier subprocesses run without ACT/ORC addresses or credentials,
behind dead proxies and with uv offline. Builder-written test code is arbitrary
Python executed by the verifier on the host that runs ACT; it must not inherit the
credentials or discovery the Navigator process itself holds. Defence in depth, not
a sandbox (HOME stays readable).
"""
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from navigator.contracts.enums import Role
from navigator.runtime.drivers.base import DriverRequest, DriverResult
from navigator.runtime.phases import (_DEAD_PROXY, _is_sealed_key, _run_sealed,
                                      run_build_and_verify, sealed_verifier_env)
from navigator.runtime.worktree import WorktreeHandle
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import CommandRejected

# addresses and credentials the daemon holds; none may reach a verifier child
SECRETS = {
    "NAVIGATOR_ACT_BASE_URL": "http://127.0.0.1:8090",
    "NAVIGATOR_ORC_BASE_URL": "http://127.0.0.1:8091",
    "NAVIGATOR_REPO": "/some/repo",
    "NAVIGATOR_TELEGRAM_BOT_TOKEN": "tg-secret-token",
    "ACT_API_KEY": "act-secret",
    "ORC_API_KEY": "orc-secret",
    "act_base_url": "http://act-lower",
    "SOME_SERVICE_TOKEN": "svc-secret",
    "lowercase_token": "lower-secret",
    "DB_PASSWORD": "db-secret",
    "AWS_SECRET": "aws-secret",
    "OPENAI_API_KEY": "openai-secret",
    "TOKEN": "bare-secret",
}
SECRET_VALUES = tuple(SECRETS.values())
# look-alikes the old substring rule (ACT_/ORC_/TOKEN) would have dropped: kept
KEPT = {
    "KEEP_ME": "kept",
    "CONTACT_X": "ops@example.invalid",
    "EXACT_MATCH": "yes",
    "TOKENIZERS_PARALLELISM": "false",
    "TOKEN_BUDGET": "100",
}


@pytest.fixture
def leaky_env(monkeypatch):
    """Parent environment as the daemon would have it: URLs, tokens, a corporate
    proxy and a NO_PROXY that would bypass it for localhost."""
    for key, value in {**SECRETS, **KEPT}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HTTP_PROXY", "http://corp-proxy:3128")
    monkeypatch.setenv("https_proxy", "http://corp-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.delenv("UV_OFFLINE", raising=False)
    return dict(SECRETS)


def _assert_sealed(env: dict):
    for key in SECRETS:
        assert key not in env, key
    for value in SECRET_VALUES:
        assert value not in env.values(), value
    for key in env:
        upper = key.upper()
        assert not upper.startswith(("NAVIGATOR_", "ACT_", "ORC_")), key
        assert not upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")), key
        assert upper != "TOKEN", key
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == _DEAD_PROXY, key
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""
    assert env["UV_OFFLINE"] == "1"
    assert env["PATH"] == os.environ["PATH"]


def _assert_kept(env: dict):
    for key, value in KEPT.items():
        assert env.get(key) == value, key


# ---- the env itself -----------------------------------------------------------

def test_sealed_env_drops_secrets_sets_dead_proxies_keeps_the_rest(leaky_env):
    env = sealed_verifier_env()
    _assert_sealed(env)
    _assert_kept(env)
    assert _DEAD_PROXY == "http://127.0.0.1:9"
    assert env.get("HOME") == os.environ.get("HOME")
    # the parent process is untouched
    assert os.environ["NAVIGATOR_ACT_BASE_URL"] == "http://127.0.0.1:8090"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
    assert "UV_OFFLINE" not in os.environ


def test_sealed_env_from_explicit_base_is_pure():
    base = {"PATH": "/usr/bin", "HOME": "/h", "EDITOR": "vi",
            "NAVIGATOR_X": "1", "ACT_URL": "u", "ORC_THING": "o", "act_api_key": "a",
            "GH_TOKEN": "t", "Some_Token": "s", "DB_PASSWORD": "p", "AWS_SECRET": "w",
            "OPENAI_API_KEY": "k", "TOKEN": "b", "token": "c",
            "CONTACT_EMAIL": "e", "EXACT_MATCH": "x", "TOKENIZERS_PARALLELISM": "false",
            "MY_ACT_URL": "m", "TOKEN_BUDGET": "9", "UV_OFFLINE": "0"}
    env = sealed_verifier_env(base)
    assert set(env) == {"PATH", "HOME", "EDITOR", "CONTACT_EMAIL", "EXACT_MATCH",
                        "TOKENIZERS_PARALLELISM", "MY_ACT_URL", "TOKEN_BUDGET",
                        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "NO_PROXY", "no_proxy", "UV_OFFLINE"}
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/h" and env["EDITOR"] == "vi"
    assert env["UV_OFFLINE"] == "1"  # forced on, whatever the parent had
    assert base["NAVIGATOR_X"] == "1" and base["UV_OFFLINE"] == "0"  # input not mutated


@pytest.mark.parametrize("key", [
    "NAVIGATOR_REPO", "navigator_repo", "ACT_API_KEY", "act_base_url", "ORC_URL", "orc_x",
    "GITHUB_TOKEN", "github_token", "HF_TOKEN", "TOKEN", "token", "DB_PASSWORD",
    "db_password", "JWT_SECRET", "OPENAI_API_KEY", "openai_api_key",
])
def test_sealed_keys(key):
    assert _is_sealed_key(key)


@pytest.mark.parametrize("key", [
    "PATH", "HOME", "CONTACT_X", "EXACT_MATCH", "TOKENIZERS_PARALLELISM", "TOKEN_BUDGET",
    "MY_ACT_URL", "REACTOR", "SECRET_SANTA", "PASSWORD_HINT", "API_KEY_NAME", "TOKENS",
])
def test_kept_keys(key):
    assert not _is_sealed_key(key)


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
    _assert_kept(runner.calls[0]["env"])
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


# ---- the real CommandRunner ----------------------------------------------------

_DUMP_ENV = "import json, os\nprint(json.dumps(dict(os.environ)))\n"


def test_child_spawned_by_the_real_command_runner_sees_the_sealed_env(tmp_path, leaky_env):
    # CommandRunner takes no env= and derives the child's env from os.environ; the
    # replacement in _run_sealed is what seals it. Exercised end to end: real
    # runner, real allowlist and worktree checks, real child.
    (tmp_path / "dump_env.py").write_text(_DUMP_ENV)
    runner = CommandRunner(allowlist=["python3"], worktree_root=tmp_path)
    before = dict(os.environ)
    r = _run_sealed(runner, ["python3", "dump_env.py"], tmp_path)
    assert r.returncode == 0, r.stderr
    child = json.loads(r.stdout)
    _assert_sealed(child)
    _assert_kept(child)
    assert child["CONTACT_X"] == KEPT["CONTACT_X"]
    assert child["UV_OFFLINE"] == "1"
    assert child["PATH"] == os.environ["PATH"]
    for value in SECRET_VALUES:
        assert value not in r.stdout
    assert dict(os.environ) == before


def test_real_command_runner_still_rejects_through_run_sealed(tmp_path, leaky_env):
    runner = CommandRunner(allowlist=["python3"], worktree_root=tmp_path)
    before = dict(os.environ)
    with pytest.raises(CommandRejected):
        _run_sealed(runner, ["rm", "-rf", "x"], tmp_path)
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
    r = subprocess.run([sys.executable, "-c", _DUMP_ENV],
                       env=sealed_verifier_env(), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    child = json.loads(r.stdout)
    _assert_sealed(child)
    _assert_kept(child)
    for value in SECRET_VALUES:
        assert value not in r.stdout


# ---- env-routed HTTP clients ---------------------------------------------------
# urllib (like requests/httpx/curl) picks its proxy from the environment. With the
# dead proxy set and NO_PROXY cleared, a client that trusts the environment cannot
# reach a local listener. This proves the proxy settings route such clients into
# the void; it does not and cannot prove a raw socket is blocked (not a sandbox).

class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_listener():
    """A real HTTP server on an ephemeral 127.0.0.1 port, standing in for ACT."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/v1/agent/runs"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_FETCH = """
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resp:
        print("REACHED", resp.status, resp.read().decode())
except Exception as e:
    print("BLOCKED", type(e).__name__, e)
"""

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _fetch(url: str, env: dict) -> str:
    r = subprocess.run([sys.executable, "-c", _FETCH, url], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_env_routed_http_clients_reach_a_local_listener_plainly_but_not_sealed(local_listener):
    # plain: the parent env minus any proxy of its own, so the control reaches the
    # listener directly and the only difference in the second run is the sealing
    plain = {k: v for k, v in os.environ.items() if k.upper() not in _PROXY_ENV_KEYS}
    reached = _fetch(local_listener, plain)
    assert reached.startswith("REACHED 200 hello"), reached
    blocked = _fetch(local_listener, sealed_verifier_env(plain))
    assert blocked.startswith("BLOCKED"), blocked
    assert "REACHED" not in blocked
