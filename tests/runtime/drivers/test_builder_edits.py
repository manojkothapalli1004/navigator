"""N9: the builder may send search/replace edits (file_edits) for existing files;
the driver resolves them into full-content file_writes so phases.py and every
downstream check (scope, skip gate, checkpoints, verification) are unchanged.
"""
import json
import pytest
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.base import DriverRequest
from navigator.contracts.enums import Role
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.errors import DriverParseError

BASE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


def _run(response, files=None, role=Role.builder, prompts=None):
    def query_fn(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return json.dumps(response)
    d = ClaudeSDKDriver(query_fn=query_fn, budget_guard=BudgetGuard(10.0))
    ctx = {} if files is None else {"files": files}
    return d.run_role(DriverRequest(role=role, context=ctx, worktree_path="/tmp/w"))


def test_single_edit_is_resolved_into_a_full_file_write():
    res = _run({"file_edits": [["a.py", "    return 1\n", "    return 10\n"]],
                "commands": [], "summary": "s"}, files={"a.py": BASE})
    assert res.file_writes == [("a.py", "def a():\n    return 10\n\n\ndef b():\n    return 2\n")]
    assert res.commands == [] and res.summary == "s"


def test_two_edits_to_one_file_apply_in_order_and_yield_one_write():
    # the second edit only matches text produced by the first
    res = _run({"file_edits": [["a.py", "return 1\n", "return 11\n"],
                               ["a.py", "return 11\n", "return 111\n"]],
                "summary": "s"}, files={"a.py": BASE})
    assert len(res.file_writes) == 1
    rel, content = res.file_writes[0]
    assert rel == "a.py"
    assert "return 111\n" in content and "return 11\n" not in content
    assert content.endswith("def b():\n    return 2\n")


def test_edits_to_two_files_each_become_one_write_in_first_edit_order():
    res = _run({"file_edits": [["b.py", "y = 2\n", "y = 20\n"],
                               ["a.py", "return 2\n", "return 20\n"],
                               ["b.py", "x = 1\n", "x = 10\n"]],
                "summary": "s"}, files={"a.py": BASE, "b.py": "x = 1\ny = 2\n"})
    assert res.file_writes == [("b.py", "x = 10\ny = 20\n"),
                               ("a.py", "def a():\n    return 1\n\n\ndef b():\n    return 20\n")]


def test_edit_whose_old_occurs_zero_times_is_rejected_with_the_count():
    with pytest.raises(DriverParseError,
                       match=r"file_edits\[0\] for 'a\.py': old text occurs 0 times") as ei:
        _run({"file_edits": [["a.py", "return 99\n", "x"]], "summary": "s"}, files={"a.py": BASE})
    assert BASE not in str(ei.value)  # never the full file


def test_edit_whose_old_occurs_twice_is_rejected_with_index_path_and_count():
    files = {"a.py": BASE, "dup.py": "x = 1\nx = 1\n"}
    with pytest.raises(DriverParseError,
                       match=r"file_edits\[1\] for 'dup\.py': old text occurs 2 times"):
        _run({"file_edits": [["a.py", "return 1\n", "return 3\n"],
                             ["dup.py", "x = 1", "y = 1"]], "summary": "s"}, files=files)


def test_edit_with_empty_old_is_rejected():
    with pytest.raises(DriverParseError, match="non-empty"):
        _run({"file_edits": [["a.py", "", "x"]], "summary": "s"}, files={"a.py": BASE})


def test_edit_on_a_new_file_is_rejected_pointing_at_file_writes():
    with pytest.raises(DriverParseError, match=r"file_edits\[0\].*'new\.py'.*use file_writes"):
        _run({"file_edits": [["new.py", "a", "b"]], "summary": "s"}, files={"new.py": None})


def test_edit_on_a_path_not_in_context_files_is_rejected():
    with pytest.raises(DriverParseError, match=r"file_edits\[0\].*'b\.py'.*not in context\.files"):
        _run({"file_edits": [["b.py", "a", "b"]], "summary": "s"}, files={"a.py": BASE})


def test_edit_without_any_context_files_is_rejected():
    with pytest.raises(DriverParseError, match="not in context.files"):
        _run({"file_edits": [["a.py", "a", "b"]], "summary": "s"})


def test_same_path_in_file_edits_and_file_writes_is_rejected():
    with pytest.raises(DriverParseError, match="both file_edits and file_writes"):
        _run({"file_writes": [["a.py", "whole\n"]],
              "file_edits": [["a.py", "return 1\n", "return 2\n"]],
              "summary": "s"}, files={"a.py": BASE})


def test_mixed_new_file_write_and_edit_to_existing_file():
    res = _run({"file_writes": [["new.py", "print(1)\n"]],
                "file_edits": [["a.py", "    return 2\n", "    return 22\n"]],
                "commands": [], "summary": "s"}, files={"a.py": BASE, "new.py": None})
    # explicit writes first, resolved edits appended after them
    assert res.file_writes == [("new.py", "print(1)\n"),
                               ("a.py", "def a():\n    return 1\n\n\ndef b():\n    return 22\n")]


@pytest.mark.parametrize("entry", [
    ["a.py", "old"],
    ["a.py", "old", "new", "extra"],
    ["a.py", 1, "new"],
    ["a.py", "old", None],
    "a.py",
    None,
    {"relpath": "a.py", "old": "x", "new": "y"},
])
def test_malformed_file_edit_entry_is_rejected(entry):
    with pytest.raises(DriverParseError, match="file_edits"):
        _run({"file_edits": [entry], "summary": "s"}, files={"a.py": BASE})


def test_file_edits_that_is_not_a_list_is_rejected():
    with pytest.raises(DriverParseError, match="file_edits"):
        _run({"file_edits": "a.py old new", "summary": "s"}, files={"a.py": BASE})


def test_non_builder_roles_may_not_send_file_edits():
    with pytest.raises(DriverParseError, match="verifier is not permitted to write files"):
        _run({"commands": [], "file_edits": [["a.py", "a", "b"]], "summary": "s"},
             files={"a.py": BASE}, role=Role.verifier)
    with pytest.raises(DriverParseError, match="planner is not permitted to write files"):
        _run({"proposals": [], "file_edits": [["a.py", "a", "b"]], "summary": "s"},
             files={"a.py": BASE}, role=Role.planner)


def test_builder_without_file_edits_behaves_as_before():
    res = _run({"file_writes": [["new.py", "x\n"]], "commands": [], "summary": "s"},
               files={"new.py": None})
    assert res.file_writes == [("new.py", "x\n")]
    res = _run({"file_writes": [], "file_edits": [], "commands": [], "summary": "s"},
               files={"a.py": BASE})
    assert res.file_writes == []


def test_builder_prompt_tells_the_model_to_use_file_edits_for_existing_files():
    prompts = []
    _run({"file_writes": [], "commands": [], "summary": "s"}, files={"a.py": BASE}, prompts=prompts)
    p = prompts[0]
    assert "file_edits" in p and "[[relpath, old, new], ...]" in p
    assert "verbatim" in p.lower() and "exactly once" in p
    assert "file_writes only for NEW files" in p
    # the full-rewrite path and the testing instructions are still spelled out
    assert "FULL new file content" in p and "never elide unchanged code" in p
    assert "new or changed behaviour" in p and "context.test_files" in p
