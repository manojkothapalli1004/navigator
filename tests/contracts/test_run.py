from navigator.contracts.run import Run, Checkpoint
from navigator.contracts.enums import RunStatus, RunPhase

def test_run_defaults_and_checkpoints():
    r = Run(proposal_id="proposal-1", base_commit_sha="abc", branch="nav/run-1",
            worktree_path="/tmp/x", correlation_id="corr-1")
    assert r.id.startswith("run-")
    assert r.status == RunStatus.executing
    assert r.phase == RunPhase.pending
    assert r.checkpoints == []
    r.checkpoints.append(Checkpoint(sha="deadbeef", label="pre-edit-1"))
    assert r.checkpoints[0].sha == "deadbeef"
