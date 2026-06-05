from datetime import datetime, timezone
import subprocess
from pathlib import Path
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run, Checkpoint
from navigator.contracts.outcome import Outcome, Rollback
from navigator.contracts.event import Event
from navigator.contracts.enums import RunPhase, RunStatus, ApprovalGate, ApprovalDecision, Severity
from navigator.runtime.worktree import create_worktree, preserve_failed, remove_worktree
from navigator.runtime.checkpoint import diff_artifact
from navigator.runtime.phases import run_build_and_verify
from navigator.ids import new_id

def _emit(act, run, kind, severity=Severity.info, **payload):
    act.emit_event(Event(kind=kind, severity=severity, correlation_id=run.correlation_id, payload=payload))

def execute_proposal(proposal: Proposal, *, act, orc, driver, runner, settings) -> Run:
    # Ensure the proposal is registered in ACT so _repo_for can resolve it
    if proposal.id not in act.proposals:
        act.create_proposal(proposal)
    run = Run(proposal_id=proposal.id, base_commit_sha=proposal.base_commit_sha,
              branch=f"nav/{new_id('run')}", correlation_id=new_id("corr"))
    run.branch = f"nav/{run.id}"
    act.create_run(run)
    handle = create_worktree(repo=Path(proposal.repo), base_commit_sha=proposal.base_commit_sha,
                             run_id=run.id, worktree_root=Path(settings.worktree_root))
    run.worktree_path = str(handle.path)
    run.phase = RunPhase.building; act.update_run(run); _emit(act, run, "run.building")
    try:
        result = run_build_and_verify(driver=driver, worktree=handle, run_id=run.id, runner=runner)
    except Exception as e:
        preserve_failed(handle)
        run.status = RunStatus.failed; run.ended_at = datetime.now(timezone.utc); act.update_run(run)
        _write_outcome(orc, run, status="failed", summary=f"exception: {e}",
                       lessons=[f"build/verify raised: {e}"], preserved=handle.path)
        _emit(act, run, "run.failed", Severity.error, error=str(e)); return run
    # Correction 2: normal top-level import of Checkpoint (not dynamic __import__)
    run.checkpoints = [Checkpoint(sha=c, label="cp") for c in result.checkpoints]
    if result.verify_returncode != 0:
        preserve_failed(handle)
        run.status = RunStatus.failed; run.ended_at = datetime.now(timezone.utc); act.update_run(run)
        _write_outcome(orc, run, status="failed", summary="verification failed",
                       lessons=[f"verify rc={result.verify_returncode}: {result.verify_output[:200]}"],
                       verification_notes=result.verify_output[:500], preserved=handle.path,
                       checkpoints_made=len(result.checkpoints))
        _emit(act, run, "run.failed", Severity.error); return run
    run.phase = RunPhase.awaiting_review; act.update_run(run)
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    # Correction 1: store diff in event payload, not as a transient Run attribute
    diff = diff_artifact(handle.path, proposal.base_commit_sha)
    _emit(act, run, "run.awaiting_review", diff=diff)
    return run

def _merge_approved(act, run) -> bool:
    appr = act.list_approvals(target=run.id, gate=ApprovalGate.merge, decision=ApprovalDecision.approved)
    return len(appr) > 0

def merge_after_approval(run: Run, *, act, orc, driver, runner, settings) -> Run:
    if not _merge_approved(act, run):
        return run  # never auto-merge
    run.phase = RunPhase.merging; act.update_run(run)
    repo = _repo_for(run, act)
    subprocess.run(["git","merge","--no-ff","-m",f"navigator merge {run.id}", run.branch],
                   cwd=repo, check=True, capture_output=True, text=True)
    run.phase = RunPhase.finalizing; run.status = RunStatus.completed
    run.ended_at = datetime.now(timezone.utc); act.update_run(run)
    _write_outcome(orc, run, status="completed", summary="merged after approval",
                   lessons=["change merged"], section="decisions")
    _emit(act, run, "run.completed")
    return run

def _repo_for(run, act) -> str:
    # worktree_path = <root>/<run_id>; repo is recorded on the proposal
    prop = act.proposals[run.proposal_id]
    return prop.repo

def _write_outcome(orc, run, *, status, summary, lessons, verification_notes=None,
                   preserved=None, checkpoints_made=0, section=None):
    out = Outcome(run_id=run.id, status=status, summary=summary, lessons=lessons,
                  verification_notes=verification_notes, provenance=f"navigator {run.id}",
                  rollback=Rollback(checkpoints_made=checkpoints_made,
                                    worktree_preserved=preserved is not None,
                                    preserved_path=str(preserved) if preserved else None))
    target_section = section or ("known_failures" if status == "failed" else "status")
    note = f"[{run.id}] {summary} | lessons: {'; '.join(lessons)}"
    orc.apply_memory_update(section=target_section, note=note, replace=False)
    return out
