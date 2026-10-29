import json
import pytest
from navigator.config import Settings
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.base import DriverRequest
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.errors import BudgetExceeded, DriverParseError

PROPOSAL = {"repo": "trader", "title": "t", "rationale": "r",
            "intended_changes": "c", "affected_files": ["a.py"], "risk_tier": "low"}

def make_driver(response, max_cost_usd=2.0, est=0.25, prompts=None):
    def query_fn(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        return response if isinstance(response, str) else json.dumps(response)
    return ClaudeSDKDriver(query_fn=query_fn, budget_guard=BudgetGuard(max_cost_usd),
                           estimated_cost_per_call_usd=est)

def test_planner_parses_strict_json_proposals():
    d = make_driver({"proposals": [PROPOSAL], "summary": "planned 1"})
    result = d.run_role(DriverRequest(role=Role.planner, context={"repo": "trader"}))
    assert result.role == Role.planner
    assert result.proposals == [PROPOSAL]
    assert result.summary == "planned 1"
    assert result.file_writes == [] and result.commands == []

def test_builder_parses_file_writes_and_commands():
    d = make_driver({"file_writes": [["a.py", "print(1)\n"]],
                     "commands": [["uv", "run", "pytest", "-q"]], "summary": "built"})
    result = d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w"))
    assert result.file_writes == [("a.py", "print(1)\n")]
    assert result.commands == [["uv", "run", "pytest", "-q"]]

def test_verifier_parses_commands():
    d = make_driver({"commands": [["uv", "run", "pytest", "-q"]], "summary": "verify"})
    result = d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w"))
    assert result.commands == [["uv", "run", "pytest", "-q"]]
    assert result.file_writes == []

def test_prompt_contains_role_context_and_worktree():
    prompts = []
    d = make_driver({"commands": [], "summary": ""}, prompts=prompts)
    d.run_role(DriverRequest(role=Role.builder, context={"repo": "trader"}, worktree_path="/tmp/w1"))
    assert len(prompts) == 1
    assert "builder" in prompts[0]
    assert "trader" in prompts[0]
    assert "/tmp/w1" in prompts[0]

def test_planner_proposal_missing_required_key_rejected():
    # live haiku regression 2026-07-13: model invented its own keys \u2192 KeyError in loop
    bad = {k: v for k, v in PROPOSAL.items() if k != "intended_changes"}
    d = make_driver({"proposals": [bad], "summary": "s"})
    with pytest.raises(DriverParseError, match="intended_changes"):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_planner_proposal_wrong_types_rejected():
    bad = dict(PROPOSAL, affected_files="not-a-list")
    d = make_driver({"proposals": [bad], "summary": "s"})
    with pytest.raises(DriverParseError, match="affected_files"):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_planner_prompt_spells_out_proposal_schema():
    prompts = []
    d = make_driver({"proposals": [], "summary": ""}, prompts=prompts)
    d.run_role(DriverRequest(role=Role.planner, context={}))
    for key in ("title", "rationale", "intended_changes", "affected_files"):
        assert key in prompts[0]

def test_budget_checked_before_call():
    prompts = []
    d = make_driver({"proposals": [], "summary": ""}, max_cost_usd=0.1, est=0.25, prompts=prompts)
    with pytest.raises(BudgetExceeded):
        d.run_role(DriverRequest(role=Role.planner, context={}))
    assert prompts == []  # query_fn never invoked once budget would be exceeded

def test_spend_recorded_after_each_call():
    d = make_driver({"proposals": [], "summary": ""}, max_cost_usd=0.6, est=0.25)
    d.run_role(DriverRequest(role=Role.planner, context={}))
    assert d.budget_guard.spent_usd == 0.25
    d.run_role(DriverRequest(role=Role.planner, context={}))
    assert d.budget_guard.spent_usd == 0.5
    with pytest.raises(BudgetExceeded):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_prose_wrapped_json_rejected():
    d = make_driver('Sure! Here is the plan: {"proposals": [], "summary": ""}')
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_markdown_fenced_json_rejected():
    d = make_driver('```json\n{"proposals": [], "summary": ""}\n```')
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_non_object_json_rejected():
    d = make_driver("[1, 2, 3]")
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_planner_missing_proposals_key_rejected():
    d = make_driver({"summary": "no proposals key"})
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.planner, context={}))

def test_malformed_file_write_entry_rejected():
    d = make_driver({"file_writes": [["only_path.py"]], "summary": ""})
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w"))

def test_role_permissions_enforced_on_output():
    # planner is read-only: file_writes in its response must be rejected
    d = make_driver({"proposals": [], "file_writes": [["a.py", "x"]], "summary": ""})
    with pytest.raises(DriverParseError):
        d.run_role(DriverRequest(role=Role.planner, context={}))
    # verifier has no write permission either
    d2 = make_driver({"commands": [], "file_writes": [["a.py", "x"]], "summary": ""})
    with pytest.raises(DriverParseError):
        d2.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w"))

def test_parse_error_includes_head_and_tail_of_raw():
    # a 120-char head alone hid the failure mode of a live 2026-07-15 planner
    # cycle; the tail shows whether the response was truncated mid-JSON
    raw = '{"proposals": [' + '{"title": "x"}, ' * 50 + '{"title": "cut off'
    d = make_driver(raw)
    with pytest.raises(DriverParseError, match="cut off"):
        d.run_role(DriverRequest(role=Role.planner, context={}))


def test_role_specific_query_fns_route_by_role():
    calls = []
    def make(name, reply):
        def fn(prompt):
            calls.append(name); return reply
        return fn
    drv = ClaudeSDKDriver(
        query_fn=make("default", '{"commands": [["git", "status"]], "summary": "s"}'),
        role_query_fns={Role.planner: make("planner", '{"proposals": [], "summary": "s"}')},
        budget_guard=BudgetGuard(10))
    drv.run_role(DriverRequest(role=Role.planner, context={}))
    drv.run_role(DriverRequest(role=Role.verifier, context={}))
    assert calls == ["planner", "default"]


def test_verifier_command_given_as_a_string_is_split_into_argv():
    # live 2026-10-10: a Sonnet verifier returned "uv run pytest tests/x.py -q" as one
    # string; the run died as a parse exception instead of verifying
    drv = ClaudeSDKDriver(query_fn=lambda p: json.dumps(
        {"commands": ["uv run pytest tests/x.py -q", ["git", "status"]], "summary": "s"}),
        budget_guard=BudgetGuard(10))
    res = drv.run_role(DriverRequest(role=Role.verifier, context={}))
    assert res.commands == [["uv", "run", "pytest", "tests/x.py", "-q"], ["git", "status"]]


def test_planner_prompt_tells_the_model_to_follow_the_repo_brief():
    seen = {}
    drv = ClaudeSDKDriver(query_fn=lambda p: seen.setdefault("p", p) and '{"proposals": [], "summary": "s"}',
                          budget_guard=BudgetGuard(10))
    drv.run_role(DriverRequest(role=Role.planner, context={"snapshot": {"repo_brief": "x"}}))
    assert "repo_brief" in seen["p"] and "context_files" in seen["p"]


def test_builder_is_told_to_test_the_behaviour_it_changes():
    # live 2026-10-11: a trader regime-gate run changed entry logic and added no tests;
    # the suite still passed because nothing exercised the new path
    seen = {}
    drv = ClaudeSDKDriver(query_fn=lambda p: seen.setdefault("p", p) and
                          '{"file_writes": [], "commands": [], "summary": "s"}',
                          budget_guard=BudgetGuard(10))
    drv.run_role(DriverRequest(role=Role.builder, context={}))
    assert "tests" in seen["p"] and "new or changed behaviour" in seen["p"]
    # test files may be ADDED even though they aren't in context.files
    assert "must be listed in context.files" not in seen["p"]
    assert "context.test_files" in seen["p"]


# --- from_settings: model fallback chain wiring (operator decision 2026-10-15) --------

def test_from_settings_wires_role_models_fallback_chain_and_one_shared_cooldown_dict():
    built = []
    def fake_make(*, model, timeout_s, fallbacks, cooldown_s, cooldowns):
        built.append({"model": model, "timeout_s": timeout_s, "fallbacks": fallbacks,
                      "cooldown_s": cooldown_s, "cooldowns": cooldowns})
        return lambda prompt: model
    s = Settings(sdk_model="claude-fable-5-1",
                 sdk_role_models={"planner": "claude-sonnet-5-5", "verifier": "claude-sonnet-5-5"},
                 sdk_model_fallbacks=["claude-opus-5-5", "claude-sonnet-5-5"],
                 sdk_model_cooldown_s=120, sdk_timeout_s=42.0)
    drv = ClaudeSDKDriver.from_settings(s, budget_guard=BudgetGuard(1.5), make_query_fn=fake_make)
    assert isinstance(drv, ClaudeSDKDriver)
    assert drv.budget_guard.max_cost_usd == 1.5
    assert drv.query_fn("x") == "claude-fable-5-1"
    assert drv.role_query_fns[Role.planner]("x") == "claude-sonnet-5-5"
    assert drv.role_query_fns[Role.verifier]("x") == "claude-sonnet-5-5"
    assert Role.builder not in drv.role_query_fns
    assert len(built) == 3
    for b in built:
        assert b["fallbacks"] == ("claude-opus-5-5", "claude-sonnet-5-5")
        assert isinstance(b["fallbacks"], tuple)
        assert b["cooldown_s"] == 120 and b["timeout_s"] == 42.0
        assert b["cooldowns"] is built[0]["cooldowns"]  # ONE dict shared by every role
    assert isinstance(built[0]["cooldowns"], dict) and built[0]["cooldowns"] == {}


def test_from_settings_empty_fallbacks_disable_the_chain():
    built = []
    def fake_make(*, model, **kw):
        built.append(kw)
        return lambda prompt: model
    s = Settings(sdk_model="m", sdk_model_fallbacks=[])
    ClaudeSDKDriver.from_settings(s, budget_guard=BudgetGuard(1.0), make_query_fn=fake_make)
    assert built == [{"timeout_s": 300.0, "fallbacks": (), "cooldown_s": 3600,
                      "cooldowns": built[0]["cooldowns"]}]


def test_from_settings_defaults_to_the_real_sdk_query_factory(monkeypatch):
    import sys, types
    mod = types.ModuleType("claude_agent_sdk")
    mod.query = None; mod.ClaudeAgentOptions = None; mod.ResultMessage = None
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    drv = ClaudeSDKDriver.from_settings(Settings(sdk_model="m", sdk_role_models={"planner": "p"}),
                                        budget_guard=BudgetGuard(1.0))
    assert callable(drv.query_fn) and callable(drv.role_query_fns[Role.planner])


def test_from_settings_requires_sdk_extra(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(ValueError, match="claude-agent-sdk"):
        ClaudeSDKDriver.from_settings(Settings(sdk_model="m"), budget_guard=BudgetGuard(1.0))


# --- builder tools (operator decision 2026-10-28): eyes, never hands ----------------------

def _recording_make(built):
    def fake_make(*, model, **kw):
        built.append(dict(kw, model=model))
        return lambda prompt: model
    return fake_make


def test_from_settings_builder_tools_on_wires_only_the_builder():
    built = []
    s = Settings(sdk_model="claude-fable-5-1",
                 sdk_role_models={"planner": "claude-sonnet-5-5", "verifier": "claude-sonnet-5-5"},
                 sdk_model_fallbacks=["claude-opus-5-5", "claude-sonnet-5-5"],
                 sdk_model_cooldown_s=120, sdk_timeout_s=42.0, builder_tools=True,
                 sdk_builder_max_turns=25, sdk_builder_timeout_s=901.0)
    drv = ClaudeSDKDriver.from_settings(s, budget_guard=BudgetGuard(1.5),
                                        make_query_fn=_recording_make(built))
    assert Role.builder in drv.role_query_fns
    assert drv.role_query_fns[Role.builder]("x") == "claude-fable-5-1"
    tooled = [b for b in built if "tools" in b]
    assert len(tooled) == 1
    assert tooled[0]["tools"] == ("Read", "Grep", "Glob")
    assert tooled[0]["max_turns"] == 25
    assert tooled[0]["timeout_s"] == 901.0
    assert tooled[0]["model"] == "claude-fable-5-1"
    assert tooled[0]["fallbacks"] == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert tooled[0]["cooldown_s"] == 120
    others = [b for b in built if "tools" not in b]
    assert len(others) == 3  # planner, verifier, default
    assert all("max_turns" not in b and b["timeout_s"] == 42.0 for b in others)
    assert all(b["cooldowns"] is built[0]["cooldowns"] for b in built)  # ONE shared dict


def test_from_settings_builder_tools_use_the_builder_role_model_and_the_larger_timeout():
    built = []
    s = Settings(sdk_model="claude-fable-5-1", sdk_role_models={"builder": "claude-opus-5-5"},
                 builder_tools=True, sdk_timeout_s=1500.0, sdk_builder_timeout_s=900.0)
    drv = ClaudeSDKDriver.from_settings(s, budget_guard=BudgetGuard(1.5),
                                        make_query_fn=_recording_make(built))
    tooled = [b for b in built if "tools" in b]
    assert len(tooled) == 1
    assert tooled[0]["model"] == "claude-opus-5-5"
    assert tooled[0]["timeout_s"] == 1500.0  # never less than the tool-less cap
    assert drv.role_query_fns[Role.builder]("x") == "claude-opus-5-5"


def test_from_settings_flag_off_never_passes_tools():
    # NEGATIVE: with the flag off the wiring is exactly today's
    built = []
    s = Settings(sdk_model="m", sdk_role_models={"planner": "p", "verifier": "v"},
                 builder_tools=False)
    drv = ClaudeSDKDriver.from_settings(s, budget_guard=BudgetGuard(1.0),
                                        make_query_fn=_recording_make(built))
    assert Role.builder not in drv.role_query_fns
    assert all("tools" not in b and "max_turns" not in b for b in built)
    assert len(built) == 3


def test_run_role_passes_the_worktree_as_cwd_to_a_builder_fn_that_accepts_it():
    seen = {}
    def builder_fn(prompt, *, cwd=None):
        seen["cwd"] = cwd
        return json.dumps({"file_writes": [["a.py", "x\n"]], "commands": [], "summary": "b"})
    def plain_fn(prompt):
        return json.dumps({"commands": [], "summary": "v"})
    d = ClaudeSDKDriver(query_fn=plain_fn, budget_guard=BudgetGuard(2.0),
                        role_query_fns={Role.builder: builder_fn})
    res = d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7"))
    assert seen["cwd"] == "/tmp/w7"
    assert res.file_writes == [("a.py", "x\n")]
    # plain_fn takes no cwd: the verifier call must not raise
    d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w7"))


def test_run_role_never_passes_cwd_to_planner_or_verifier_even_if_they_accept_it():
    # NEGATIVE: cwd reaches the builder only
    seen = []
    def fn(prompt, *, cwd="unset"):
        seen.append(cwd)
        return json.dumps({"proposals": [], "commands": [], "summary": "s"})
    d = ClaudeSDKDriver(query_fn=fn, budget_guard=BudgetGuard(2.0))
    d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w"))
    d.run_role(DriverRequest(role=Role.planner, context={}))
    assert seen == ["unset", "unset"]
    # a builder request without a worktree: no worktree, no cwd
    d.run_role(DriverRequest(role=Role.builder, context={}))
    assert seen[-1] == "unset"


def test_run_role_builder_with_a_plain_query_fn_still_works():
    # the (prompt)-only fake is called without cwd
    d = make_driver({"file_writes": [["a.py", "1\n"]], "commands": [], "summary": "ok"})
    res = d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w"))
    assert res.file_writes == [("a.py", "1\n")]


def test_from_settings_builder_tools_refuse_a_worktree_root_under_users():
    built = []
    with pytest.raises(ValueError, match="NAVIGATOR_WORKTREE_ROOT"):
        ClaudeSDKDriver.from_settings(
            Settings(sdk_model="m", builder_tools=True, worktree_root="/Users/someone/wt"),
            budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make(built))
    assert built == []  # refused before any query fn is built
    ClaudeSDKDriver.from_settings(
        Settings(sdk_model="m", builder_tools=False, worktree_root="/Users/someone/wt"),
        budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make(built))
    assert all("tools" not in b for b in built)
    built.clear()
    ClaudeSDKDriver.from_settings(
        Settings(sdk_model="m", builder_tools=True, worktree_root="/tmp/navigator-worktrees"),
        budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make(built))
    assert len([b for b in built if "tools" in b]) == 1


# --- builder tests (build 2): one bounded run_tests tool on the tooled builder session -----

def test_from_settings_builder_tests_requires_builder_tools():
    built = []
    with pytest.raises(ValueError, match="NAVIGATOR_BUILDER_TESTS"):
        ClaudeSDKDriver.from_settings(Settings(sdk_model="m", builder_tools=False, builder_tests=True),
                                      budget_guard=BudgetGuard(1.0),
                                      make_query_fn=_recording_make(built))
    assert built == []  # nothing wired


def test_from_settings_builder_tests_still_refuse_a_worktree_root_under_users():
    built = []
    with pytest.raises(ValueError, match="NAVIGATOR_WORKTREE_ROOT"):
        ClaudeSDKDriver.from_settings(
            Settings(sdk_model="m", builder_tools=True, builder_tests=True,
                     worktree_root="/Users/someone/wt"),
            budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make(built))
    assert built == []


def test_from_settings_builder_tests_on_stores_the_bound_and_wires_the_same_tooled_fn():
    built = []
    drv = ClaudeSDKDriver.from_settings(
        Settings(sdk_model="m", builder_tools=True, builder_tests=True, builder_test_runs=3,
                 worktree_root="/tmp/navigator-worktrees"),
        budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make(built))
    assert drv.builder_tests is True
    assert drv.builder_test_runs == 3
    assert drv.last_test_runs == []
    tooled = [b for b in built if "tools" in b]
    assert len(tooled) == 1 and tooled[0]["tools"] == ("Read", "Grep", "Glob")  # 2a adds no query fn
    # flag off: the driver carries the defaults
    off = ClaudeSDKDriver.from_settings(
        Settings(sdk_model="m", builder_tools=True, worktree_root="/tmp/navigator-worktrees"),
        budget_guard=BudgetGuard(1.0), make_query_fn=_recording_make([]))
    assert off.builder_tests is False and off.builder_test_runs == 5


def test_run_role_builder_with_tests_passes_an_mcp_server_and_the_tool_name(monkeypatch):
    from navigator.runtime.drivers import claude_sdk_driver as csd
    seen, server_args = {}, {}
    def builder_fn(prompt, *, cwd=None, mcp_servers=None, mcp_tools=()):
        seen.update(cwd=cwd, mcp_servers=mcp_servers, mcp_tools=mcp_tools)
        return json.dumps({"file_writes": [["a.py", "x\n"]], "commands": [], "summary": "b"})
    def plain_fn(prompt):
        return json.dumps({"commands": [], "summary": "v"})
    def fake_server(runner, run_dir, allowed_commands, *, max_runs, budget=None, worktree=None):
        server_args.update(args=(runner, run_dir, allowed_commands), max_runs=max_runs,
                           budget=budget, worktree=worktree)
        return "SERVER", ["c1"]
    monkeypatch.setattr(csd, "make_run_tests_server", fake_server)
    d = ClaudeSDKDriver(query_fn=plain_fn, budget_guard=BudgetGuard(2.0),
                        role_query_fns={Role.builder: builder_fn}, builder_tests=True,
                        builder_test_runs=2)
    assert d.last_test_runs == []
    res = d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7",
                                   runner="RUNNER", run_dir="/tmp/w7",
                                   allowed_commands=("uv run pytest",)))
    assert server_args == {"args": ("RUNNER", "/tmp/w7", ("uv run pytest",)), "max_runs": 2,
                           "budget": None, "worktree": "/tmp/w7"}
    assert seen == {"cwd": "/tmp/w7", "mcp_servers": {"navigator": "SERVER"},
                    "mcp_tools": ("mcp__navigator__run_tests",)}
    assert d.last_test_runs == ["c1"]
    assert res.file_writes == [("a.py", "x\n")]


def test_run_role_never_builds_a_test_server_for_planner_or_verifier(monkeypatch):
    # NEGATIVE: the role check comes first, exactly as in build 1
    from navigator.runtime.drivers import claude_sdk_driver as csd
    seen, called = [], []
    def fn(prompt, **kw):
        seen.append(kw)
        return json.dumps({"proposals": [], "commands": [], "summary": "s"})
    monkeypatch.setattr(csd, "make_run_tests_server",
                        lambda *a, **k: called.append((a, k)) or ("S", []))
    d = ClaudeSDKDriver(query_fn=fn, budget_guard=BudgetGuard(2.0), builder_tests=True,
                        builder_test_runs=2)
    d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w",
                             runner="RUNNER", run_dir="/tmp/w", allowed_commands=("uv run pytest",)))
    d.run_role(DriverRequest(role=Role.planner, context={}, runner="RUNNER", run_dir="/tmp/w",
                             allowed_commands=("uv run pytest",)))
    assert called == []
    assert seen == [{}, {}]  # no cwd, no mcp_servers
    assert d.last_test_runs == []


def test_run_role_without_runner_or_with_tests_off_is_build_1_behaviour(monkeypatch):
    # NEGATIVE: the tool needs the flag, a runner, a run_dir AND a fn that accepts mcp_servers
    from navigator.runtime.drivers import claude_sdk_driver as csd
    called = []
    monkeypatch.setattr(csd, "make_run_tests_server",
                        lambda *a, **k: called.append((a, k)) or ("S", []))
    reply = json.dumps({"file_writes": [["a.py", "x\n"]], "commands": [], "summary": "b"})
    seen = []
    def builder_fn(prompt, *, cwd=None, mcp_servers=None, mcp_tools=()):
        seen.append({"cwd": cwd, "mcp_servers": mcp_servers, "mcp_tools": mcp_tools})
        return reply
    def cwd_only_fn(prompt, *, cwd=None):
        seen.append({"cwd": cwd})
        return reply
    def plain_fn(prompt):
        return json.dumps({"commands": [], "summary": "v"})
    cwd_only = {"cwd": "/tmp/w7", "mcp_servers": None, "mcp_tools": ()}
    # builder_tests on, but no runner on the request
    d = ClaudeSDKDriver(query_fn=plain_fn, budget_guard=BudgetGuard(10.0),
                        role_query_fns={Role.builder: builder_fn}, builder_tests=True)
    d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7"))
    assert seen[-1] == cwd_only and called == []
    # builder_tests on, runner set, but no run_dir
    d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7",
                             runner="RUNNER", allowed_commands=("uv run pytest",)))
    assert seen[-1] == cwd_only and called == []
    # builder_tests off, runner and run_dir set
    d_off = ClaudeSDKDriver(query_fn=plain_fn, budget_guard=BudgetGuard(10.0),
                            role_query_fns={Role.builder: builder_fn}, builder_tests=False)
    d_off.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7",
                                 runner="RUNNER", run_dir="/tmp/w7", allowed_commands=("uv run pytest",)))
    assert seen[-1] == cwd_only and called == []
    # a builder fn that accepts cwd but not mcp_servers: cwd only, no server built
    d_cwd = ClaudeSDKDriver(query_fn=plain_fn, budget_guard=BudgetGuard(10.0),
                            role_query_fns={Role.builder: cwd_only_fn}, builder_tests=True)
    d_cwd.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7",
                                 runner="RUNNER", run_dir="/tmp/w7", allowed_commands=("uv run pytest",)))
    assert seen[-1] == {"cwd": "/tmp/w7"} and called == []
    assert d.last_test_runs == [] and d_off.last_test_runs == [] and d_cwd.last_test_runs == []


# --- build 2b: the request's per-build budget reaches the server; the builder is told ---------

def test_run_role_passes_the_request_budget_to_the_server(monkeypatch):
    from navigator.runtime.drivers import claude_sdk_driver as csd
    seen = []
    def fake_server(runner, run_dir, allowed_commands, *, max_runs, budget=None, worktree=None):
        seen.append({"max_runs": max_runs, "budget": budget, "worktree": worktree})
        return "SERVER", []
    monkeypatch.setattr(csd, "make_run_tests_server", fake_server)
    def builder_fn(prompt, *, cwd=None, mcp_servers=None, mcp_tools=()):
        return json.dumps({"file_writes": [["a.py", "x\n"]], "commands": [], "summary": "b"})
    d = ClaudeSDKDriver(query_fn=builder_fn, budget_guard=BudgetGuard(10.0), builder_tests=True,
                        builder_test_runs=3)
    shared = {"used": 2}
    d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7", runner="RUNNER",
                             run_dir="/tmp/w7", allowed_commands=("uv run pytest",), test_budget=shared))
    assert seen[-1]["budget"] is shared  # the SAME dict: phases counts across repair turns
    assert seen[-1] == {"max_runs": 3, "budget": shared, "worktree": "/tmp/w7"}
    d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w7", runner="RUNNER",
                             run_dir="/tmp/w7", allowed_commands=("uv run pytest",)))
    assert seen[-1]["budget"] is None  # no per-build counter: 2a's fresh per-call budget
    assert len(seen) == 2


def test_builder_prompt_says_run_tests_describes_the_worktree_before_its_edits():
    prompts = []
    d = make_driver({"file_writes": [], "commands": [], "summary": ""}, prompts=prompts)
    d.run_role(DriverRequest(role=Role.builder, context={}, worktree_path="/tmp/w"))
    assert "If a run_tests tool is available" in prompts[0]
    assert "BEFORE your edits" in prompts[0]
    assert prompts[0].endswith("still reply with the full JSON object.")
    # the sentence is the builder's alone
    d.run_role(DriverRequest(role=Role.verifier, context={}, worktree_path="/tmp/w"))
    assert "run_tests" not in prompts[1]
