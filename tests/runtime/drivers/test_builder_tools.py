"""Builder test runner (build 2): the one tool by which a tooled builder may run anything.

Every spawn goes through phases._run_sealed and the CommandRunner; the tool itself only
picks the allowlisted pytest entry, adds -q and validated paths, bounds calls per turn and
caps the output tail. No network, no SDK: claude_agent_sdk is stubbed where needed.
"""
import asyncio
import os
import sys
import time
import types
from pathlib import Path

import pytest

from navigator.config import DEFAULT_ALLOWLIST
from navigator.runtime.drivers.builder_tools import (OUTPUT_CHARS, QUALIFIED_TOOL, SERVER_NAME,
                                                     TOOL_DESCRIPTION, TOOL_NAME,
                                                     make_run_tests_server, pytest_entry,
                                                     run_tests_handler, validate_paths)
from navigator.safety.command_runner import CommandResult
from navigator.safety.errors import CommandRejected


class _FakeRunner:
    def __init__(self, result=None, allowlist=("uv run pytest",)):
        self.allowlist = list(allowlist)
        self.calls = []
        self.result = result or CommandResult(0, "3 passed", "", None)

    def run(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))
        return self.result


def _call(runner, run_dir, paths=None, *, allowed_commands=("uv run pytest",), budget=None,
          max_runs=5, calls=None, output_chars=OUTPUT_CHARS):
    """One handler call; returns (text, budget, calls)."""
    budget = {"used": 0} if budget is None else budget
    calls = [] if calls is None else calls
    args = {} if paths is None else {"paths": paths}
    res = asyncio.run(run_tests_handler(
        args, runner=runner, run_dir=str(run_dir), allowed_commands=list(allowed_commands),
        budget=budget, max_runs=max_runs, calls=calls, lock=asyncio.Lock(),
        output_chars=output_chars))
    assert list(res) == ["content"] and res["content"][0]["type"] == "text"
    return res["content"][0]["text"], budget, calls


def test_tool_names_and_description():
    assert SERVER_NAME == "navigator" and TOOL_NAME == "run_tests"
    assert QUALIFIED_TOOL == f"mcp__{SERVER_NAME}__{TOOL_NAME}"
    assert "pytest" in TOOL_DESCRIPTION and "[]" in TOOL_DESCRIPTION and "NOW" in TOOL_DESCRIPTION


# --- (a) the pytest entry -------------------------------------------------------------

def test_pytest_entry_is_the_first_allowlist_entry_with_a_pytest_token():
    assert pytest_entry(["git status", "python3 -m py_compile", "uv run pytest"]) == ["uv", "run", "pytest"]
    assert pytest_entry(["git status"]) is None
    assert pytest_entry(["uv run pytest-cov"]) is None  # split tokens, not substrings
    assert pytest_entry(()) is None and pytest_entry(None) is None
    assert pytest_entry(("uv run --extra dev pytest",)) == ["uv", "run", "--extra", "dev", "pytest"]
    # the live default allowlist: the bare entry comes first, as in phases._run_full_suite
    assert pytest_entry(DEFAULT_ALLOWLIST) == ["pytest"]


# --- (b) path validation ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["-x", "-k", "--basetemp=/Users/x", "@args.txt", "/abs",
                                 "../x", "a/../../x", "", 42])
def test_validate_paths_rejects_flags_argfiles_absolute_and_escapes(tmp_path, bad):
    with pytest.raises(ValueError, match="rejected path") as exc:
        validate_paths([bad], tmp_path)
    assert str(tmp_path) not in str(exc.value)


def test_validate_paths_rejects_a_non_list(tmp_path):
    for bad in ("tests/test_a.py", None, 7, {"paths": []}):
        with pytest.raises(ValueError, match="rejected path"):
            validate_paths(bad, tmp_path)


def test_validate_paths_accepts_paths_under_the_run_dir(tmp_path):
    assert validate_paths(["tests/test_a.py", "tests"], tmp_path) == ["tests/test_a.py", "tests"]
    assert validate_paths([], tmp_path) == []
    assert validate_paths(["tests/test_a.py::test_x"], tmp_path) == ["tests/test_a.py::test_x"]


# --- (c) happy path ------------------------------------------------------------------

def test_handler_runs_the_allowlisted_pytest_entry_in_the_run_dir(tmp_path):
    # allowed_commands given explicitly: pytest_entry(DEFAULT_ALLOWLIST) picks the bare entry
    runner = _FakeRunner()
    text, budget, calls = _call(runner, tmp_path, ["tests/test_a.py"],
                                allowed_commands=["uv run pytest"])
    assert runner.calls == [(["uv", "run", "pytest", "-q", "tests/test_a.py"], Path(tmp_path))]
    assert text.startswith("rc=0\n3 passed")
    assert budget == {"used": 1}
    assert calls == [{"argv": ["uv", "run", "pytest", "-q", "tests/test_a.py"], "text": text[:200]}]


def test_handler_runs_the_whole_suite_for_an_empty_or_missing_paths_argument(tmp_path):
    runner = _FakeRunner()
    _call(runner, tmp_path, [])
    _call(runner, tmp_path, None)
    _call(runner, tmp_path, [], allowed_commands=["git status", "uv run --extra dev pytest"])
    assert [argv for argv, _ in runner.calls] == [["uv", "run", "pytest", "-q"],
                                                  ["uv", "run", "pytest", "-q"],
                                                  ["uv", "run", "--extra", "dev", "pytest", "-q"]]


# --- (d) no pytest entry: nothing runs -------------------------------------------------

def test_handler_refuses_when_no_pytest_command_is_allowlisted(tmp_path):
    runner = _FakeRunner(allowlist=["git status"])
    text, budget, calls = _call(runner, tmp_path, [], allowed_commands=["git status"])
    assert text == "rejected: no pytest command allowlisted"
    assert runner.calls == []
    assert budget["used"] == 1
    assert calls == [{"argv": None, "text": text}]


def test_handler_rejects_a_bad_path_before_running_anything(tmp_path):
    runner = _FakeRunner()
    text, budget, calls = _call(runner, tmp_path, ["@args.txt"])
    assert text == "rejected: rejected path: @args.txt"
    assert runner.calls == [] and budget["used"] == 1
    assert calls == [{"argv": None, "text": text}]


# --- (d2) overlapping calls are serialised ---------------------------------------------

def test_overlapping_calls_are_serialised_by_the_lock(tmp_path):
    class _Slow:
        allowlist = ["uv run pytest"]
        def __init__(self):
            self.spans = []
        def run(self, argv, cwd):
            enter = time.monotonic()
            time.sleep(0.2)
            self.spans.append((enter, time.monotonic()))
            return CommandResult(0, "ok", "", None)
    runner = _Slow()
    budget, calls = {"used": 0}, []

    async def both():
        lock = asyncio.Lock()
        kw = dict(runner=runner, run_dir=str(tmp_path), allowed_commands=["uv run pytest"],
                  budget=budget, max_runs=5, calls=calls, lock=lock)
        return await asyncio.gather(run_tests_handler({"paths": []}, **kw),
                                    run_tests_handler({"paths": []}, **kw))

    results = asyncio.run(both())
    assert len(runner.spans) == 2
    first, second = sorted(runner.spans)
    assert second[0] >= first[1]  # the second run entered only after the first left
    assert all(r["content"][0]["text"].startswith("rc=0") for r in results)
    assert budget["used"] == 2 and len(calls) == 2


# --- (e) runner rejections and errors never escape -------------------------------------

def test_handler_reports_a_command_rejected_by_the_runner(tmp_path):
    class _Rejecting(_FakeRunner):
        def run(self, argv, cwd):
            raise CommandRejected("command not on allowlist: uv run pytest -q")
    text, budget, calls = _call(_Rejecting(), tmp_path, [])
    assert text.startswith("rejected: command not on allowlist")
    assert budget["used"] == 1
    assert calls[0]["argv"] == ["uv", "run", "pytest", "-q"]


def test_handler_never_lets_a_runner_exception_escape(tmp_path):
    class _Boom(_FakeRunner):
        def run(self, argv, cwd):
            raise OSError("spawn failed")
    text, budget, calls = _call(_Boom(), tmp_path, [])
    assert text == "error: OSError: spawn failed"
    assert budget["used"] == 1 and calls[0]["argv"] == ["uv", "run", "pytest", "-q"]


# --- (f) run dir under /Users ----------------------------------------------------------

@pytest.mark.parametrize("run_dir", ["/Users/someone/wt", "/System/Volumes/Data/Users/x"])
def test_handler_refuses_a_run_dir_under_users(run_dir):
    runner = _FakeRunner()
    text, budget, calls = _call(runner, run_dir, [])
    assert text.startswith("rejected: run directory under /Users")
    assert runner.calls == []
    assert budget["used"] == 1
    assert calls == [{"argv": None, "text": text}]


# --- (g) the per-turn budget -----------------------------------------------------------

def test_budget_bounds_runs_per_turn(tmp_path):
    runner = _FakeRunner()
    budget = {"used": 0}
    text1, _, _ = _call(runner, tmp_path, [], budget=budget, max_runs=1)
    assert text1.startswith("rc=0")
    text2, _, calls = _call(runner, tmp_path, [], budget=budget, max_runs=1)
    assert text2 == "test-run budget exhausted (1 per turn)"
    assert len(runner.calls) == 1  # the runner ran exactly once
    assert budget == {"used": 1}
    assert calls == [{"argv": None, "text": text2}]


def test_a_rejected_call_spends_the_budget_too(tmp_path):
    runner = _FakeRunner()
    budget = {"used": 0}
    text, _, _ = _call(runner, tmp_path, ["-x"], budget=budget, max_runs=1)
    assert text == "rejected: rejected path: -x"
    assert budget == {"used": 1} and runner.calls == []
    text, _, _ = _call(runner, tmp_path, [], budget=budget, max_runs=1)
    assert text == "test-run budget exhausted (1 per turn)"
    assert runner.calls == [] and budget == {"used": 1}


# --- (h) output cap --------------------------------------------------------------------

def test_output_tail_is_capped(tmp_path):
    runner = _FakeRunner(result=CommandResult(0, "x" * 10_000, "", None))
    text, _, calls = _call(runner, tmp_path, [], output_chars=50)
    assert len(text) == len("rc=0\n") + 50
    assert text.endswith("x")
    assert len(calls[0]["text"]) <= 200
    # stderr follows stdout (as in the verifier's own output); the tail keeps the end
    runner = _FakeRunner(result=CommandResult(1, "out", "err-tail", None))
    text, _, _ = _call(runner, tmp_path, [], output_chars=5)
    assert text == "rc=1\n-tail"
    assert OUTPUT_CHARS == 4000


# --- (i) sealing -----------------------------------------------------------------------

def test_runner_is_spawned_sealed_and_os_environ_is_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_ACT_BASE_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("ACT_API_KEY", "act-secret")
    monkeypatch.delenv("UV_OFFLINE", raising=False)
    seen = {}
    class _Env(_FakeRunner):
        def run(self, argv, cwd):
            seen["env"] = dict(os.environ)
            return super().run(argv, cwd)
    text, _, _ = _call(_Env(), tmp_path, [])
    assert text.startswith("rc=0")
    env = seen["env"]
    assert "NAVIGATOR_ACT_BASE_URL" not in env and "ACT_API_KEY" not in env
    assert "act-secret" not in env.values()
    assert env["UV_OFFLINE"] == "1"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:9"
    # the daemon's own environment is back once the call returns
    assert os.environ["NAVIGATOR_ACT_BASE_URL"] == "http://127.0.0.1:8090"
    assert os.environ["ACT_API_KEY"] == "act-secret"
    assert "UV_OFFLINE" not in os.environ


# --- (j) the MCP server ----------------------------------------------------------------

def _stub_sdk(monkeypatch, recorded):
    mod = types.ModuleType("claude_agent_sdk")
    def tool(name, description, schema):  # a decorator FACTORY, like the real SDK's
        def deco(fn):
            recorded.append((name, description, schema))
            return fn
        return deco
    def create_sdk_mcp_server(name, version="1.0.0", tools=None):
        return {"name": name, "version": version, "tools": tools}
    mod.tool = tool
    mod.create_sdk_mcp_server = create_sdk_mcp_server
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)


def test_make_run_tests_server_registers_one_tool_bound_to_the_runner(tmp_path, monkeypatch):
    recorded = []
    _stub_sdk(monkeypatch, recorded)
    runner = _FakeRunner()
    server, calls = make_run_tests_server(runner, str(tmp_path), ["uv run pytest"], max_runs=2)
    assert recorded == [("run_tests", TOOL_DESCRIPTION, {"paths": list[str]})]
    assert server["name"] == "navigator" and len(server["tools"]) == 1
    assert calls == []
    res = asyncio.run(server["tools"][0]({"paths": []}))
    assert res["content"][0]["text"].startswith("rc=0\n3 passed")
    assert runner.calls == [(["uv", "run", "pytest", "-q"], Path(tmp_path))]
    assert runner.calls[0][0][-1] == "-q"
    assert calls == [{"argv": ["uv", "run", "pytest", "-q"], "text": res["content"][0]["text"][:200]}]
    # the per-server budget: the third call this turn runs nothing
    asyncio.run(server["tools"][0]({"paths": ["tests"]}))
    res = asyncio.run(server["tools"][0]({"paths": []}))
    assert res["content"][0]["text"] == "test-run budget exhausted (2 per turn)"
    assert len(runner.calls) == 2 and len(calls) == 3


def test_make_run_tests_server_honours_a_shared_budget(tmp_path, monkeypatch):
    _stub_sdk(monkeypatch, [])
    runner = _FakeRunner()
    server, calls = make_run_tests_server(runner, str(tmp_path), ("uv run pytest",), max_runs=2,
                                          budget={"used": 2})
    res = asyncio.run(server["tools"][0]({"paths": []}))
    assert res["content"][0]["text"] == "test-run budget exhausted (2 per turn)"
    assert runner.calls == [] and len(calls) == 1


def test_make_run_tests_server_requires_the_sdk_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(ValueError, match="claude-agent-sdk"):
        make_run_tests_server(_FakeRunner(), str(tmp_path), ["uv run pytest"], max_runs=2)
