import json
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.base import DriverRequest
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard


def _prompt_for(role, response):
    seen = {}
    def query(prompt):
        seen["prompt"] = prompt
        return json.dumps(response)
    d = ClaudeSDKDriver(query_fn=query, budget_guard=BudgetGuard(10.0))
    d.run_role(DriverRequest(role=role, context={"allowed_commands": ["git log"]},
                             worktree_path="/tmp/x"))
    return seen["prompt"]


def test_verifier_prompt_lists_git_log_flags_including_n():
    prompt = _prompt_for(Role.verifier, {"commands": [], "summary": "ok"})
    line = next(l for l in prompt.splitlines() if l.startswith("git flag allowlist"))
    log_part = line.split("git log: ", 1)[1]
    assert "-n <1-50>" in log_part
    assert "--max-count <1-50>" in log_part
    assert "--oneline" in log_part
    assert "git diff: " in line and "git status: " in line


def test_builder_prompt_lists_git_flags():
    prompt = _prompt_for(Role.builder, {"file_writes": [], "commands": [], "summary": "ok"})
    assert "git log: " in prompt and "-n <1-50>" in prompt


def test_planner_prompt_has_no_git_flag_line():
    prompt = _prompt_for(Role.planner, {"proposals": [], "summary": "ok"})
    assert "git flag allowlist" not in prompt
