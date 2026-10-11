import json
import pytest
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
    # live haiku regression 2026-07-13: model invented its own keys → KeyError in loop
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
