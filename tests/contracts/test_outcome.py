import json
from navigator.contracts.outcome import Outcome, Rollback

def test_outcome_supports_failure_lessons_and_rollback():
    o = Outcome(run_id="run-1", status="failed", summary="tests failed",
                lessons=["py_compile passed but pytest failed on import"],
                verification_notes="uv run pytest -> 1 failed",
                provenance="navigator run-1",
                rollback=Rollback(checkpoints_made=2, checkpoints_restored=1,
                                  worktree_preserved=True, preserved_path="/tmp/nav/run-1"))
    assert o.id.startswith("outcome-")
    assert o.rollback.worktree_preserved is True
    assert "import" in o.lessons[0]

def test_outcome_without_code_sha_loads_from_old_jsonl():
    old_line = '{"id":"outcome-1","schema_version":1,"run_id":"run-1","proposal_id":"p",' \
               '"status":"completed","summary":"s","lessons":[]}'
    o = Outcome(**json.loads(old_line))
    assert o.code_sha is None
    assert o.run_id == "run-1"

def test_outcome_with_code_sha_round_trips():
    o = Outcome(run_id="run-1", status="failed", summary="s", code_sha="abc123")
    again = Outcome(**json.loads(json.dumps(o.model_dump())))
    assert again.code_sha == "abc123"
