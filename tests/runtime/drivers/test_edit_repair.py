"""Part B (spec 2026-11-08): exactly one repair turn on an edit-application error."""
import json
import pytest
from navigator.runtime import phases
from navigator.runtime.drivers import claude_sdk_driver as drv_mod
from navigator.runtime.drivers.base import DriverRequest
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.errors import BudgetExceeded, DriverParseError
from navigator.contracts.enums import Role

BASE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
GOOD = {"file_edits": [["a.py", "    return 1\n", "    return 10\n"]], "summary": "s"}
BAD = {"file_edits": [["a.py", "def zzz():\n    return 1\n", "    return 10\n"]], "summary": "s"}
DUP = {"file_edits": [["a.py", "return", "ret"]], "summary": "s"}


class Q:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.replies[len(self.prompts) - 1]
        return r if isinstance(r, str) else json.dumps(r)


def _mk(tmp_path, q, guard=None, files=None, affected=("a.py",), extra=None):
    (tmp_path / "a.py").write_text(BASE)
    d = ClaudeSDKDriver(query_fn=q, budget_guard=guard or BudgetGuard(10.0))
    ctx = {"files": files if files is not None else {"a.py": BASE},
           "proposal": {"affected_files": list(affected)}, **(extra or {})}
    return d, DriverRequest(role=Role.builder, context=ctx, worktree_path=str(tmp_path))


def test_edit_not_found_gets_one_repair_turn_then_applies(tmp_path):
    q = Q([BAD, GOOD])
    d, req = _mk(tmp_path, q)
    res = d.run_role(req)
    assert res.file_writes == [("a.py", BASE.replace("    return 1\n", "    return 10\n"))]
    assert len(q.prompts) == 2
    err = "file_edits[0] for 'a.py': old text occurs 0 times (must be exactly 1); len(old)=24 len(new)=14"
    assert err in q.prompts[1]
    assert q.prompts[1].startswith(q.prompts[0])
    assert json.dumps(BAD) in q.prompts[1]
    assert "def a():\n    return 1\n" in q.prompts[1].split(err, 1)[1]    # verbatim excerpt after the error


def test_edit_found_twice_repair_prompt_has_excerpt_around_first_occurrence(tmp_path):
    text = "".join(f"line {i}\n" for i in range(200)) + "dup = 1\n" + "".join(f"mid {i}\n" for i in range(200)) \
        + "dup = 1\n" + "".join(f"end {i}\n" for i in range(200))
    q = Q([{"file_edits": [["a.py", "dup = 1", "dup = 2"]], "summary": "s"},
           {"file_edits": [["a.py", "dup = 1\nmid 0", "dup = 2\nmid 0"]], "summary": "s"}])
    d, req = _mk(tmp_path, q, files={"a.py": text})
    (tmp_path / "a.py").write_text(text)
    res = d.run_role(req)
    assert len(q.prompts) == 2 and "occurs 2 times" in q.prompts[1]
    tail = q.prompts[1].split("occurs 2 times", 1)[1]
    assert "line 199\ndup = 1\nmid 0" in tail and "end 5" not in tail
    assert res.file_writes[0][1].count("dup = 2") == 1


def test_second_edit_failure_is_raised_no_third_call(tmp_path):
    q = Q([BAD, BAD, GOOD])
    d, req = _mk(tmp_path, q)
    with pytest.raises(DriverParseError, match="occurs 0 times"):
        d.run_role(req)
    assert len(q.prompts) == 2


@pytest.mark.parametrize("reply,kw", [
    ("not json", {}),
    ({"file_edits": [["a.py", "x"]], "summary": "s"}, {}),
    ({"file_writes": [["big.py", "x"]], "summary": "s"}, {"large": True}),
])
def test_non_edit_parse_errors_get_no_repair_turn(tmp_path, reply, kw):
    q = Q([reply, GOOD])
    extra = {"large_files": {"big.py": {"chars": 70000, "lines": 9, "sha256": "x"}}} if kw else None
    d, req = _mk(tmp_path, q, extra=extra)
    with pytest.raises(DriverParseError):
        d.run_role(req)
    assert len(q.prompts) == 1


def test_query_fn_exception_gets_no_repair_turn(tmp_path):
    calls = []

    def q(prompt):
        calls.append(prompt)
        raise RuntimeError("boom")
    d, req = _mk(tmp_path, q)
    with pytest.raises(RuntimeError):
        d.run_role(req)
    assert len(calls) == 1


def test_repair_turn_counts_against_budget_guard(tmp_path):
    class G(BudgetGuard):
        checks = records = 0

        def check_before_call(self, usd):
            G.checks += 1
            super().check_before_call(usd)

        def record_spend(self, usd):
            G.records += 1
            super().record_spend(usd)
    q = Q([BAD, GOOD])
    d, req = _mk(tmp_path, q, guard=G(10.0))
    d.run_role(req)
    assert (G.checks, G.records) == (2, 2)
    # a guard that refuses the second call raises its own error and the second call never happens
    q2 = Q([BAD, GOOD])
    d2, req2 = _mk(tmp_path, q2, guard=BudgetGuard(0.4))
    with pytest.raises(BudgetExceeded):
        d2.run_role(req2)
    assert len(q2.prompts) == 1


def test_repair_prompt_without_anchor_has_error_only(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/other.py").write_text("SECRET_TEXT = 1\n")
    (tmp_path / ".env").write_text("TOKEN=SECRET_TEXT\n")
    for rel in ("pkg/other.py", ".env", "missing.py"):
        q = Q([{"file_edits": [[rel, "SECRET_TEXT", "x"]], "summary": "s"}, GOOD])
        d, req = _mk(tmp_path, q)
        d.run_role(req)
        assert len(q.prompts) == 2
        assert "edit-application error" in q.prompts[1]
        assert q.prompts[1].count("SECRET_TEXT") == 1     # only inside the echoed first reply


def test_repair_error_messages_contain_no_file_text(tmp_path):
    q = Q([BAD, {"file_edits": [["a.py", "SEKRIT_OLD", "SEKRIT_NEW"]], "summary": "s"}])
    d, req = _mk(tmp_path, q)
    with pytest.raises(DriverParseError) as ei:
        d.run_role(req)
    msg = str(ei.value)
    assert "SEKRIT" not in msg and "return 2" not in msg and "def b" not in msg


def test_edit_apply_error_carries_anchor_attributes_not_in_message(tmp_path):
    with pytest.raises(drv_mod._EditApplyError) as ei:
        d, req = _mk(tmp_path, Q([BAD]))
        d.run_role(req) if False else d._parse(Role.builder, json.dumps(BAD), files={"a.py": BASE})
    e = ei.value
    assert (e.rel, e.index, e.old) == ("a.py", 0, "def zzz():\n    return 1\n") and "zzz" not in str(e)


def test_repair_reuses_test_budget_and_recomputes_editable(tmp_path, monkeypatch):
    made = []

    def fake_server(runner, run_dir, allowed, max_runs, budget, worktree):
        calls = []
        made.append(calls)
        return object(), calls
    monkeypatch.setattr(drv_mod, "make_run_tests_server", fake_server)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/one.txt").write_text("alpha\n")
    (tmp_path / "frontend/two.txt").write_text("beta\n")
    seen = []

    def q(prompt, cwd=None, mcp_servers=None, mcp_tools=None):
        seen.append((cwd, mcp_servers, mcp_tools))
        first = len(seen) == 1
        return json.dumps({"file_edits": [["frontend/one.txt", "nope" if first else "alpha", "A"]]
                           if first else [["frontend/two.txt", "beta", "B"]], "summary": "s"})
    d = ClaudeSDKDriver(query_fn=q, budget_guard=BudgetGuard(10.0), builder_tests=True)
    req = DriverRequest(role=Role.builder, context={"files": {}, "editable_dir": "frontend",
                                                    "proposal": {"affected_files": []}},
                        worktree_path=str(tmp_path), runner=object(), run_dir="rd",
                        allowed_commands=("pytest",), test_budget={"used": 0})
    res = d.run_role(req)
    assert res.file_writes == [("frontend/two.txt", "B\n")]    # second reply's own editable path
    assert len(made) == 1 and len(seen) == 2 and seen[0][1] is seen[1][1]
    assert d.last_test_runs is made[0]
