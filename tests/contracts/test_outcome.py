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
