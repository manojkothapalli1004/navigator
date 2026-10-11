from datetime import datetime, timezone
import subprocess
import time
from pathlib import Path
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run, Checkpoint
from navigator.contracts.outcome import Outcome, Rollback
from navigator.contracts.event import Event
from navigator.contracts.enums import RunPhase, RunStatus, ApprovalGate, ApprovalDecision, Severity
from navigator.runtime.worktree import (
    WorktreeHandle, create_worktree, preserve_failed, remove_worktree)
from navigator.runtime.checkpoint import diff_artifact
from navigator.runtime.outcome_store import OutcomeStore
from navigator.runtime.phases import run_build_and_verify
from navigator.ids import new_id
from navigator.safety.approval_engine import require_gate
from navigator.safety.circuit_breakers import CircuitBreakers

def _save(act, run: Run) -> None:
    # every persisted transition doubles as a liveness heartbeat for reconcile
    run.heartbeat_at = datetime.now(timezone.utc)
    act.update_run(run)

def _emit(act, run, kind, severity=Severity.info, **payload):
    payload = {"run_id": run.id, "proposal_id": run.proposal_id, **payload}
    act.emit_event(Event(kind=kind, severity=severity, correlation_id=run.correlation_id, payload=payload))

def execute_proposal(proposal: Proposal, *, act, orc, driver, runner, settings,
                     workdir: str = ".") -> Run:
    # Ensure the proposal is registered in ACT so _repo_for can resolve it
    if act.get_proposal(proposal.id) is None:
        proposal = act.create_proposal(proposal)
    require_gate(act, ApprovalGate.review, proposal.id)
    started = time.monotonic()
    guard = getattr(driver, "budget_guard", None)
    if guard is not None:
        guard.reset()  # the cap is per run; planner spend earlier in the cycle doesn't count
    breakers = CircuitBreakers(max_tool_failures=settings.max_tool_failures,
                               max_identical_failures=settings.max_identical_failures,
                               max_run_duration_s=settings.max_run_duration_s,
                               max_failed_tests=settings.max_failed_tests)
    # branch from the repo's current HEAD, not the proposal's base: proposals sit
    # for weeks and an old base guarantees merge conflicts at the merge gate
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proposal.repo,
                          capture_output=True, text=True).stdout.strip() or proposal.base_commit_sha
    target = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=proposal.repo,
                            capture_output=True, text=True).stdout.strip() or None
    run = Run(proposal_id=proposal.id, base_commit_sha=head, target_branch=target,
              branch="pending", correlation_id=new_id("corr"))
    run.branch = f"nav/{run.id}"
    act.create_run(run)
    try:
        handle = create_worktree(repo=Path(proposal.repo), base_commit_sha=head,
                                 run_id=run.id, worktree_root=Path(settings.worktree_root))
    except Exception as e:
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary=f"worktree creation failed: {e}",
                       lessons=[f"create_worktree raised: {e}"], settings=settings)
        _emit(act, run, "run.failed", Severity.error, error=str(e)); return run
    run.worktree_path = str(handle.path)
    run.phase = RunPhase.building; _save(act, run); _emit(act, run, "run.building")
    try:
        result = run_build_and_verify(
            driver=driver, worktree=handle, run_id=run.id, runner=runner,
            proposal={"title": proposal.title, "rationale": proposal.rationale,
                      "intended_changes": proposal.intended_changes,
                      "affected_files": list(proposal.affected_files)},
            breakers=breakers, started_monotonic=started, workdir=workdir)
    except Exception as e:
        preserve_failed(handle)
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary=f"exception: {e}",
                       lessons=[f"build/verify raised: {e}"], preserved=handle.path, settings=settings)
        _emit(act, run, "run.failed", Severity.error, error=str(e)); return run
    # Correction 2: normal top-level import of Checkpoint (not dynamic __import__)
    run.checkpoints = [Checkpoint(sha=c, label="cp") for c in result.checkpoints]
    run.verification = result.verify_log
    if guard is not None:
        run.cost_usd = round(guard.spent_usd, 4)  # driver's per-call estimate
        run.model_calls = guard.calls
    if result.verify_returncode != 0:
        preserve_failed(handle)
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary="verification failed",
                       lessons=[f"verify rc={result.verify_returncode}: {result.verify_output[:200]}"],
                       verification_notes=result.verify_output[:500], preserved=handle.path,
                       checkpoints_made=len(result.checkpoints), settings=settings)
        _emit(act, run, "run.failed", Severity.error); return run
    run.phase = RunPhase.awaiting_review; _save(act, run)
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    # Correction 1: store diff in event payload, not as a transient Run attribute
    diff = diff_artifact(handle.path, head)
    _emit(act, run, "run.awaiting_review", diff=diff, title=proposal.title,
          verification=run.verification)
    return run

def _merge_approved(act, run) -> bool:
    appr = act.list_approvals(target=run.id, gate=ApprovalGate.merge, decision=ApprovalDecision.approved)
    return len(appr) > 0

def _merge_preflight(repo: str, run: Run) -> str | None:
    """Reason the main checkout can't take the merge right now, else None. The main
    checkout is the operator's — never merge over their edits or onto another branch."""
    branch = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    if run.target_branch and branch != run.target_branch:
        return f"checkout is on {branch or 'detached HEAD'!r}, run targets {run.target_branch!r}"
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return "main checkout has uncommitted changes to tracked files"
    return None

def _git_merge(repo: str, run: Run, squash: bool, title: str):
    if not squash:
        return subprocess.run(["git", "merge", "--no-ff", "-m", f"navigator merge {run.id}", run.branch],
                              cwd=repo, capture_output=True, text=True)
    # public repos: one commit under the repo's own identity; the run branch's
    # navigator@local checkpoints never enter history
    merge = subprocess.run(["git", "merge", "--squash", run.branch], cwd=repo,
                           capture_output=True, text=True)
    if merge.returncode != 0:
        return merge
    return subprocess.run(["git", "commit", "-q", "-m", title], cwd=repo,
                          capture_output=True, text=True)

def _abort_merge(repo: str, squash: bool) -> None:
    # a squash leaves no MERGE_HEAD, so --abort can't undo it; reset --merge restores
    # the index and conflicted files while keeping unrelated local changes
    cmd = ["git", "reset", "--merge"] if squash else ["git", "merge", "--abort"]
    subprocess.run(cmd, cwd=repo, capture_output=True, text=True)

def merge_after_approval(run: Run, *, act, orc, driver, runner, settings, squash: bool = False) -> Run:
    if not _merge_approved(act, run):
        return run  # never auto-merge
    repo = _repo_for(run, act)
    blocked = _merge_preflight(repo, run)
    if blocked:
        # stays parked at the gate; the next poll retries once the operator is done
        _emit(act, run, "run.merge_blocked", Severity.warning, reason=blocked)
        return run
    run.phase = RunPhase.merging; _save(act, run)
    prop = act.get_proposal(run.proposal_id)
    merge = _git_merge(repo, run, squash, prop.title if prop else run.id)
    if merge.returncode != 0:
        _abort_merge(repo, squash)
        detail = (merge.stdout + merge.stderr).strip()[:300]
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary=f"merge failed and was aborted: {detail}",
                       lessons=["merge conflict with main — rebase or re-propose against current HEAD"],
                       preserved=run.worktree_path, settings=settings)
        _emit(act, run, "run.failed", Severity.error, error=detail)
        return run
    run.phase = RunPhase.finalizing; run.status = RunStatus.completed
    run.ended_at = datetime.now(timezone.utc); _save(act, run)
    _write_outcome(orc, run, status="completed", summary="merged after approval",
                   lessons=["change merged"], section="decisions", settings=settings)
    _cleanup_worktree(run, repo)
    if squash:
        subprocess.run(["git", "branch", "-D", run.branch], cwd=repo, capture_output=True, text=True)
    _emit(act, run, "run.completed")
    return run

def decline_run(run: Run, *, act, orc, settings) -> Run:
    run.status = RunStatus.aborted; run.phase = RunPhase.finalizing
    run.ended_at = datetime.now(timezone.utc); _save(act, run)
    _write_outcome(orc, run, status="declined", summary="merge declined by operator",
                   lessons=["operator declined the change at the merge gate"],
                   preserved=run.worktree_path, section="decisions", settings=settings)
    _emit(act, run, "run.declined")
    return run

def _cleanup_worktree(run: Run, repo: str) -> None:
    """Best-effort removal of the merged run's worktree. A cleanup failure must not
    undo an already-completed merge, so swallow errors."""
    if not run.worktree_path:
        return
    handle = WorktreeHandle(repo=Path(repo), path=Path(run.worktree_path), branch=run.branch)
    try:
        remove_worktree(handle)
    except Exception:
        pass

def _repo_for(run, act) -> str:
    # worktree_path = <root>/<run_id>; repo is recorded on the proposal
    prop = act.get_proposal(run.proposal_id)
    if prop is None:
        raise LookupError(f"proposal {run.proposal_id} not found in ACT")
    return prop.repo

def _write_outcome(orc, run, *, status, summary, lessons, verification_notes=None,
                   preserved=None, checkpoints_made=0, section=None, settings=None):
    out = Outcome(run_id=run.id, proposal_id=run.proposal_id, status=status, summary=summary,
                  lessons=lessons, verification_notes=verification_notes,
                  provenance=f"navigator {run.id}",
                  rollback=Rollback(checkpoints_made=checkpoints_made,
                                    worktree_preserved=preserved is not None,
                                    preserved_path=str(preserved) if preserved else None))
    target_section = section or ("known_failures" if status == "failed" else "status")
    note = f"[{run.id}] {summary} | lessons: {'; '.join(lessons)}"
    orc.apply_memory_update(section=target_section, note=note, replace=False)
    if settings is not None and getattr(settings, "outcome_path", None):
        try:
            OutcomeStore(settings.outcome_path).append(out)
        except OSError:
            pass  # local trail is best-effort; a store failure must not fail the run
    return out
