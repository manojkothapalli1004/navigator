from datetime import datetime, timedelta, timezone
import shlex
import subprocess
import time
from pathlib import Path
from navigator.cli import ACTOR, AUDITOR, is_audited
from navigator.observability.telegram_inbox import TELEGRAM_ACTOR
from navigator.observability.logging import get_logger
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run, Checkpoint
from navigator.contracts.outcome import Outcome, Rollback
from navigator.contracts.event import Event
from navigator.contracts.enums import RunPhase, RunStatus, ApprovalGate, ApprovalDecision, Severity
from navigator.runtime.worktree import (
    WorktreeHandle, create_worktree, preserve_failed, remove_worktree)
from navigator.runtime.checkpoint import diff_artifact
from navigator.runtime.merge_tiers import (CHECKER_ORIGIN, changed_files_for_run, classify_for_repo,
                                          repo_config_for)
from navigator.checker.store import FindingStore
from navigator.safety.command_runner import FRONTEND_SUITE
from navigator.runtime.outcome_store import OutcomeStore
from navigator.runtime.phases import (FRONTEND_SUFFIXES, NO_CHANGE_SUMMARY, reverify,
                                      run_build_and_verify, run_dir_for)
from navigator.ids import new_id
from navigator.safety.approval_engine import require_gate
from navigator.safety.circuit_breakers import CircuitBreakers
from navigator.safety.errors import QuotaExhaustedError

# Operator identities whose merge approvals the gate honours: the navigator CLI and
# the Telegram inbox. Imported from their definitions so a renamed actor cannot
# silently lock the operator out of the merge gate.
_OPERATOR_ACTORS = frozenset({ACTOR, TELEGRAM_ACTOR})
# 3b: the actor merge_poll records when it auto-approves an audited, green tier A run.
# NEVER in _OPERATOR_ACTORS: the gate honours it only when it re-proves tier A itself
# (auto_merge_ok) in the same call, so a forged auto approval merges nothing.
AUTO_ACTOR = "navigator-auto-tier-a"

# S3c: an approved merge approval that no operator gave after review would otherwise
# park the run silently forever; the operator sees this reason once per run.
_NOT_OPERATOR_REASON = "approval not from an operator after review"
# 3b: a run whose only approvals are the auto actor's failed the merge-time re-check
_AUTO_RECHECK_REASON = ("auto-merge re-check failed (stale after a rebase, not audited, or not tier A); "
                        "waiting for a fresh audit or an operator word")
# (run_id, reason) pairs already announced, the same per-run, per-reason dedupe
# NotifyingACT (N4b) applies to its notifications - here so ACT also records the
# block once rather than on every poll.
_blocked_announced: set[tuple[str, str]] = set()

def _save(act, run: Run) -> None:
    # every persisted transition doubles as a liveness heartbeat for reconcile
    run.heartbeat_at = datetime.now(timezone.utc)
    act.update_run(run)

def _emit(act, run, kind, severity=Severity.info, **payload):
    payload = {"run_id": run.id, "proposal_id": run.proposal_id, **payload}
    act.emit_event(Event(kind=kind, severity=severity, correlation_id=run.correlation_id, payload=payload))

def _emit_blocked_once(act, run, reason: str, **payload) -> bool:
    """Emit run.merge_blocked for `reason` the first time it applies to this run;
    later polls with the same reason are silent. True if an event was emitted."""
    key = (run.id, reason)
    if key in _blocked_announced:
        return False
    _blocked_announced.add(key)
    _emit(act, run, "run.merge_blocked", Severity.warning, reason=reason, **payload)
    return True

def short_sha(sha: str | None) -> str:
    return (sha or "")[:12] or "?"

def _rev_parse(cwd, rev: str) -> str:
    """Commit sha for `rev` (trusted runtime git call), or '' if it doesn't resolve."""
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], cwd=cwd,
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""

_CODE_SHA_UNSET = object()
_code_sha_cache = _CODE_SHA_UNSET

def _compute_code_sha() -> str | None:
    # trusted runtime git call (never the LLM command runner), run in the
    # navigator package's own directory so it reports Navigator's code, not the target repo
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else None

def code_sha() -> str | None:
    """Navigator's own git HEAD, computed once per process; None if git fails."""
    global _code_sha_cache
    if _code_sha_cache is _CODE_SHA_UNSET:
        try:
            _code_sha_cache = _compute_code_sha()
        except Exception:
            _code_sha_cache = None
    return _code_sha_cache

def _defer_quota(act, orc, run: Run, handle, e: QuotaExhaustedError, settings) -> None:
    """N16: an account-wide limit means the build did not happen. The run is aborted (not
    failed), its worktree removed, a 'deferred' outcome written, run.deferred emitted and
    one rebuild granted. Only the class name and resets_at are recorded: str(e) is the
    SDK's text and may carry a token-bearing URL."""
    try:
        remove_worktree(handle)
    except Exception:
        pass
    run.worktree_path = None
    run.status = RunStatus.aborted; run.phase = RunPhase.finalizing
    run.ended_at = datetime.now(timezone.utc); _save(act, run)
    _write_outcome(orc, run, status="deferred",
                   summary=f"deferred: quota exhausted until {e.resets_at or 'unknown'}",
                   lessons=[f"{type(e).__name__}: account-wide limit; rebuild granted"],
                   settings=settings)
    _emit(act, run, "run.deferred", Severity.warning, error=type(e).__name__,
          resets_at=e.resets_at)
    granted = _grant_rebuild(act, run)
    _emit(act, run, "run.rebuild_scheduled", granted=granted)
    _log.warning("execute.quota_deferred", run_id=run.id, error=type(e).__name__,
                 resets_at=e.resets_at)

def execute_proposal(proposal: Proposal, *, act, orc, driver, runner, settings,
                     workdir: str = ".") -> Run:
    """Build and verify `proposal` in a fresh worktree; the run parks at the merge gate.

    Raises QuotaExhaustedError after deferring the run (status aborted, outcome deferred,
    run.deferred event, one rebuild grant) so the caller can pause until resets_at.
    """
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
                      "affected_files": list(proposal.affected_files),
                      **_previous_failure(settings, proposal.id)},
            breakers=breakers, started_monotonic=started, workdir=workdir,
            repair_turns=getattr(settings, "builder_repair_turns", 0),
            **_frontend_kwargs(settings, proposal))
    except QuotaExhaustedError as e:
        # the run is finalized here; the re-raise lets the loop pause until resets_at
        _defer_quota(act, orc, run, handle, e, settings)
        raise
    except Exception as e:
        preserve_failed(handle)
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary=f"exception: {e}",
                       lessons=[f"build/verify raised: {e}"], preserved=handle.path, settings=settings)
        _emit(act, run, "run.failed", Severity.error, error=str(e)); return run
    repair_used = getattr(result, "repair_turns_used", 0)
    if repair_used > 0:
        _emit(act, run, "run.repair_turn", used=repair_used,
              failures=list(getattr(result, "repair_failures", [])))
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
        if result.no_change:
            # N17: an empty diff never reaches the merge gate; the lesson names the
            # fix so the next builder (which sees previous_attempt_failure) acts on it
            summary = NO_CHANGE_SUMMARY
            lessons = [f"{NO_CHANGE_SUMMARY}: the diff against base {short_sha(head)} was empty; "
                       "the next attempt must make real content changes to the affected_files "
                       "(or state in its summary that the proposal is already done)"]
        else:
            summary = "verification failed"
            lessons = [f"verify rc={result.verify_returncode}: {result.verify_output[-300:]}"]
        if repair_used > 0:
            lessons.append(f"repair turns used: {repair_used}")
        _write_outcome(orc, run, status="failed", summary=summary, lessons=lessons,
                       verification_notes=result.verify_output[-1500:], preserved=handle.path,
                       checkpoints_made=len(result.checkpoints), settings=settings)
        _emit(act, run, "run.failed", Severity.error); return run
    # the merge gate merges only this exact commit
    run.verified_sha = _rev_parse(handle.path, "HEAD") or None
    run.phase = RunPhase.awaiting_review; _save(act, run)
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    # merge tier (3a): classified from the run's own commits and recorded on the event;
    # None means unknown (a later build treats it exactly like C)
    try:
        # -z: NUL-separated and never quoted (core.quotePath=false still quoted a name holding '"' or '\\');
        # the git call sits inside the guard so an OSError is 'tier unknown', not a lost event (3a2)
        names = _git_run(handle.path, "diff", "--no-renames", "--name-only", "-z", f"{head}..HEAD")
        changed = [n for n in names.stdout.split("\x00") if n] if names.returncode == 0 else None
        tier = (classify_for_repo(changed, proposal.repo, repo_config_for(settings, proposal.repo),
                                  origin=proposal.origin)
                if changed is not None else None)
    except Exception as e:
        tier = None  # a tier failure must never lose the run.awaiting_review event or its Telegram message
        _log.warning("merge_tier.failed", run_id=run.id, error=type(e).__name__)
    # Correction 1: store diff in event payload, not as a transient Run attribute
    diff = diff_artifact(handle.path, head)
    _emit(act, run, "run.awaiting_review", diff=diff, title=proposal.title,
          verification=run.verification, verified_sha=run.verified_sha,
          repair_turns_used=repair_used, merge_tier=tier)
    return run

def _as_utc(dt: datetime) -> datetime:
    # naive timestamps (older ACT rows, fakes) are taken as UTC
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

def _approved_merge_approvals(act, run) -> list:
    """Every merge approval for the run that is in the approved state, whoever gave it."""
    return list(act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                   decision=ApprovalDecision.approved))

def _operator_approved(approvals, run, allow_auto: bool = False) -> bool:
    """True only if one of `approvals` was given by an OPERATOR AFTER the run opened
    for review (see _merge_approved for the rationale). With allow_auto the AUTO_ACTOR
    counts too, under the same decided_at rule; only merge_after_approval passes it, and
    only after it re-proved tier A itself."""
    opened = getattr(run, "heartbeat_at", None)
    for appr in approvals:
        if appr.actor not in _OPERATOR_ACTORS and not (allow_auto and appr.actor == AUTO_ACTOR):
            continue
        decided = getattr(appr, "decided_at", None)
        if decided is not None and opened is not None and _as_utc(decided) < _as_utc(opened):
            continue  # decided before the run was reviewable: cannot be the operator's verdict
        return True
    return False

def _merge_approved(act, run) -> bool:
    """True only if an OPERATOR approved the merge AFTER the run opened for review.

    Defence in depth, not a sandbox: verification runs builder-written code on the
    same host as ACT, so code could create+approve a merge approval for its own run.
    Such an approval is ignored unless its actor is one of the operator identities
    (navigator CLI or the Telegram inbox) and, when both timestamps are known, it was
    decided no earlier than the run's awaiting_review heartbeat (the _save that moved
    the run into awaiting_review; naive datetimes are treated as UTC). An approval
    without decided_at is judged on actor alone.
    """
    return _operator_approved(_approved_merge_approvals(act, run), run)

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

def _missing_branch(repo: str, run: Run) -> str | None:
    """Reason the run is unmergeable for good: its verified branch is gone. Waiting
    can't fix that, so the run fails instead of parking forever."""
    if not run.verified_sha:
        return None
    if _rev_parse(repo, f"refs/heads/{run.branch}"):
        return None
    return f"run branch missing; verified {short_sha(run.verified_sha)}"

def _verified_check(repo: str, run: Run) -> str | None:
    """Reason the run branch no longer points at the verified commit, else None.
    Runs recorded before verified_sha existed are not checked."""
    if not run.verified_sha:
        return None
    current = _rev_parse(repo, f"refs/heads/{run.branch}")
    if current != run.verified_sha:
        return (f"branch changed since verification: "
                f"{short_sha(current) if current else 'missing'} != {short_sha(run.verified_sha)}")
    return None

def _git_merge(repo: str, run: Run, squash: bool, title: str):
    # merge the exact verified commit when known, never whatever the branch points at now
    rev = run.verified_sha or run.branch
    if not squash:
        msg = f"{title} (navigator {run.id})"
        if run.verified_sha:
            msg += f" verified {short_sha(run.verified_sha)}"
        return subprocess.run(["git", "merge", "--no-ff", "-m", msg, rev],
                              cwd=repo, capture_output=True, text=True)
    # public repos: one commit under the repo's own identity; the run branch's
    # navigator@local checkpoints never enter history
    merge = subprocess.run(["git", "merge", "--squash", rev], cwd=repo,
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

def _previous_failure(settings, proposal_id: str) -> dict:
    # a retry must see why the last attempt failed, or it repeats the same mistake
    try:
        failed = [o for o in OutcomeStore(settings.outcome_path).for_proposal(proposal_id)
                  if o.status == "failed"]
    except OSError:
        return {}
    if not failed:
        return {}
    # infra errors (limits, timeouts) carry no test output; prefer a real test failure
    with_tests = [o for o in failed if o.verification_notes and "FAILED" in o.verification_notes]
    last = with_tests[-1] if with_tests else failed[-1]
    detail = (last.verification_notes or "; ".join(last.lessons) or last.summary)[-2000:]
    return {"previous_attempt_failure": f"{last.summary}: {detail}"}

def merge_after_approval(run: Run, *, act, orc, driver, runner, settings, squash: bool = False,
                         allow_auto: bool = False) -> Run:
    approvals = _approved_merge_approvals(act, run)
    by_operator = _operator_approved(approvals, run)
    auto_ok = False
    if (not by_operator and allow_auto and getattr(settings, "merge_tier_a_auto", False)
            and _operator_approved(approvals, run, allow_auto=True)):
        # the auto actor's word is accepted only when the gate itself re-proves tier A now
        auto_ok = auto_merge_ok(act, run, _repo_for(run, act), settings)
    if not (by_operator or auto_ok):
        if approvals:
            # approved, but by nobody the gate trusts (or before the run was reviewable):
            # the run stays parked, and the operator is told why - once per run
            only_auto = all(str(a.actor) == AUTO_ACTOR for a in approvals)
            _emit_blocked_once(act, run, _AUTO_RECHECK_REASON if only_auto else _NOT_OPERATOR_REASON,
                               actors=sorted({str(a.actor) for a in approvals}))
        return run  # never merge on a non-operator approval unless the gate itself re-proved tier A right now
    repo = _repo_for(run, act)
    missing = _missing_branch(repo, run)
    if missing:
        run.status = RunStatus.failed; run.phase = RunPhase.finalizing
        run.ended_at = datetime.now(timezone.utc); _save(act, run)
        _write_outcome(orc, run, status="failed", summary=missing,
                       lessons=["run branch was deleted after verification — re-propose"],
                       preserved=run.worktree_path, settings=settings)
        _emit(act, run, "run.failed", Severity.error, error=missing)
        return run
    blocked = _merge_preflight(repo, run) or _verified_check(repo, run)
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
    auto = not by_operator
    merged = _git_run(repo, "rev-parse", "HEAD")
    merged_sha = merged.stdout.strip() if merged.returncode == 0 else ""
    revert = ((f"git -C {repo} revert {merged_sha}" if squash else f"git -C {repo} revert -m 1 {merged_sha}")
              if merged_sha else f"merge sha unknown - run: git -C {repo} log -1")
    try:  # FIRST: the operator's note (Telegram goes through the ACT emit) must survive an ORC failure
        try:  # the proposal only shapes the message; a lookup failure must not cost the note
            prop = act.get_proposal(run.proposal_id)
        except Exception:
            prop = None
        _emit(act, run, "run.completed", merge_sha=merged_sha, auto=auto, revert=revert,
              origin=getattr(prop, "origin", None), title=getattr(prop, "title", None))
    except Exception as e:
        _log.warning("merge.completed_event_failed", run_id=run.id, error=type(e).__name__)
    if auto:  # close the operator's leftover pending approval: the merge is done, so `navigator reject` must not
        try:  # be able to "decline" it afterwards and print "applied on the daemon's next cycle"
            for a in act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                        decision=ApprovalDecision.pending):
                act.decide_approval(a.id, ApprovalDecision.approved, actor=AUTO_ACTOR)
        except Exception as e:
            _log.warning("merge.leftover_approval_close_failed", run_id=run.id, error=type(e).__name__)
    try:
        _write_outcome(orc, run, status="completed",
                       summary="merged automatically (tier A)" if auto else "merged after approval",
                       lessons=["change merged (tier A auto-merge)" if auto else "change merged"],
                       section="decisions", settings=settings)
    except Exception as e:  # HTTPORCClient raises on ReadTimeout (seen live); the merge is already done
        _log.warning("merge.outcome_write_failed", run_id=run.id, error=type(e).__name__)
    _cleanup_worktree(run, repo)
    if squash:
        subprocess.run(["git", "branch", "-D", run.branch], cwd=repo, capture_output=True, text=True)
    return run

_REASON_PREFIX = "reason="

def _decline_reason(act, run) -> str | None:
    """The operator's decline reason code recorded by `navigator reject --reason`: the
    first declined merge approval of the run whose note reads 'reason=<code>' yields
    <code> (up to the first ';', ':' or whitespace). None when no reason was given or the
    approvals cannot be read."""
    try:
        for a in act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                    decision=ApprovalDecision.declined):
            note = getattr(a, "note", None)
            if isinstance(note, str) and note.startswith(_REASON_PREFIX):
                rest = note[len(_REASON_PREFIX):]
                end = len(rest)
                for i, ch in enumerate(rest):
                    if ch in ";:" or ch.isspace():
                        end = i
                        break
                return rest[:end].strip() or None
    except Exception:
        return None
    return None

def decline_run(run: Run, *, act, orc, settings) -> Run:
    run.status = RunStatus.aborted; run.phase = RunPhase.finalizing
    run.ended_at = datetime.now(timezone.utc); _save(act, run)
    code = _decline_reason(act, run)
    # lessons[0] must stay the exact DECLINED_LESSON text: title_lessons matches it
    lessons = (["operator declined the change at the merge gate"]
               + ([f"decline reason: {code}"] if code else []))
    _write_outcome(orc, run, status="declined", summary="merge declined by operator",
                   lessons=lessons, preserved=run.worktree_path, section="decisions",
                   settings=settings, reason_code=code)
    _emit(act, run, "run.declined", reason_code=code)
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
                   preserved=None, checkpoints_made=0, section=None, settings=None,
                   reason_code=None):
    out = Outcome(run_id=run.id, proposal_id=run.proposal_id, status=status, summary=summary,
                  lessons=lessons, verification_notes=verification_notes,
                  provenance=f"navigator {run.id}",
                  rollback=Rollback(checkpoints_made=checkpoints_made,
                                    worktree_preserved=preserved is not None,
                                    preserved_path=str(preserved) if preserved else None),
                  code_sha=code_sha(), reason_code=reason_code)
    target_section = section or ("known_failures" if status == "failed" else "status")
    note = f"[{run.id}] {summary} | lessons: {'; '.join(lessons)}"
    orc.apply_memory_update(section=target_section, note=note, replace=False)
    if settings is not None and getattr(settings, "outcome_path", None):
        try:
            OutcomeStore(settings.outcome_path).append(out)
        except OSError:
            pass  # local trail is best-effort; a store failure must not fail the run
    return out


# ---- N12d: rebase parked sibling runs after a merge --------------------------------
# live 2026-10-19: 5 runs were built from one base; after the first merge the other 4
# conflicted textually and cost 3 extra build rounds. Instead, each sibling parked at
# the merge gate is rebased onto the new HEAD and re-verified with its own recorded
# commands (no model call). Its audit is carried forward when the normalized diff is
# unchanged. A conflict or a failed re-verify fails the run and grants exactly one
# automatic rebuild (the CLI `retry` record) so the next approval_poll rebuilds it.
_log = get_logger("navigator.executor")
_GIT_FLAGS = ["-c", "user.email=navigator@local", "-c", "user.name=navigator",
              "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
_GIT_TIMEOUT_S = 600
_CLOCK_ADVANCE_MAX_S = 3.0


def _git_run(cwd, *args) -> subprocess.CompletedProcess:
    """Trusted runtime git call with a timeout; a timeout is a failed call (rc 124)."""
    argv = ["git", *args]
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, stdout="",
                                           stderr=f"git timed out after {_GIT_TIMEOUT_S}s")


# ---- 3b: tier A auto-merge checks (recomputed from git and ACT at the moment of use) ----

def base_on_head(repo, run) -> bool:
    """run.base_commit_sha is an ancestor of the repo's current HEAD (bounded git call). False
    when the operator rewound main past the run's base: the run's own base..verified diff
    would then hide the dropped commits, and merging it would silently put them back."""
    base = getattr(run, "base_commit_sha", None)
    if not base:
        return False
    return _git_run(repo, "merge-base", "--is-ancestor", base, "HEAD").returncode == 0


def _ran_pytest(entry: dict) -> bool:
    argv = entry.get("argv_list") or str(entry.get("argv") or "").split()
    argv = [str(a) for a in argv]
    return bool(argv) and (argv[0] == "pytest" or (argv[:2] == ["uv", "run"] and "pytest" in argv))


def _ran_vitest(entry: dict) -> bool:
    """The entry ran the frontend test suite: `vitest ...`, `npx vitest ...` or `npm (run) test`
    (not merely an argv that mentions vitest, e.g. `echo vitest`)."""
    argv = [str(a) for a in (entry.get("argv_list") or str(entry.get("argv") or "").split())]
    return (argv[:1] == ["vitest"] or argv[:2] == ["npx", "vitest"]
            or argv[:2] == ["npm", "test"] or argv[:3] == ["npm", "run", "test"])


def _ran_frontend_suite(entries: list) -> bool:
    """Every FRONTEND_SUITE command has a Navigator-tagged (suite="frontend") rc-0 entry."""
    ok = {tuple(str(a) for a in e.get("argv_list") or ()) for e in entries
          if e.get("suite") == "frontend" and e.get("returncode") == 0}
    return all(tuple(a) in ok for a in FRONTEND_SUITE)


def suite_green(verification: list, changed: list, frontend_dir: str | None = None,
                origin: str | None = None) -> bool:
    """The recorded verification proves the change: a non-empty log whose LAST entry has
    returncode 0 and, when any changed path is a .py file, at least one entry ran pytest
    (argv_list[0] == "pytest", or argv_list[:2] == ["uv", "run"] with "pytest" in argv_list;
    argv_list falls back to argv.split())."""
    entries = [e for e in (verification or []) if isinstance(e, dict)]
    if not entries:
        return False
    if entries[-1].get("returncode") != 0:
        return False
    if any(str(p).endswith(".py") for p in (changed or [])):
        if not any(_ran_pytest(e) for e in entries):
            return False
    if frontend_dir and any(str(p).startswith(f"{frontend_dir}/") and str(p).endswith(FRONTEND_SUFFIXES)
                            for p in (changed or [])):
        # a repo with a frontend_dir: only Navigator's own tagged suite counts
        if not _ran_frontend_suite(entries):
            return False
    elif any(str(p).endswith(FRONTEND_SUFFIXES) for p in (changed or [])):
        if not any(_ran_vitest(e) for e in entries):
            return False
    if origin == CHECKER_ORIGIN:
        # a site-checker fix is proven by the checker itself: every filed fault gone on a preview
        if not any(e.get("suite") == "checker" and e.get("returncode") == 0 for e in entries):
            return False
    return True


def unexercised_tests(verification: list) -> bool:
    """The recorded exercise check found the run's tests pass on the base code too (2026-11-02)."""
    return any(isinstance(e, dict) and e.get("suite") == "exercise" and e.get("exercised") is False
               for e in (verification or []))


def carry_exercise(old: list, new: list) -> list:
    """A rebase re-verifies with the recorded commands only; keep the exercise result (it judged
    the run's tests against its own base) so a not-exercised run can't lose its auto-merge block."""
    kept = [e for e in (old or []) if isinstance(e, dict) and e.get("suite") == "exercise"]
    if not kept:
        return new
    new = list(new or [])
    return new[:-1] + kept + new[-1:] if new else kept


def auto_merge_ok(act, run, repo, settings) -> bool:
    """Every tier A auto-merge condition, recomputed from git and ACT at the moment of use
    (defence in depth: an approval record, an audit record or a tier payload can all be
    forged or go stale): the run is audited (is_audited), its base is on HEAD (base_on_head),
    its own commits classify as tier A, and its recorded verification is green. Any
    exception is False and logged by class name only."""
    try:
        if not is_audited(act, run) or not base_on_head(repo, run):
            return False
        if unexercised_tests(run.verification):
            return False  # its tests pass without the change: a human looks at it
        changed = changed_files_for_run(repo, run)
        if changed is None:
            return False
        prop = act.get_proposal(run.proposal_id)
        origin = prop.origin if prop is not None else None
        cfg = repo_config_for(settings, repo)
        if classify_for_repo(changed, repo, cfg, origin=origin) != "A":
            return False
        return suite_green(run.verification, changed,
                           frontend_dir=getattr(cfg, "frontend_dir", None), origin=origin)
    except Exception as e:
        _log.warning("merge.auto_check_failed", run_id=run.id, error=type(e).__name__)
        return False


def _head(wt) -> str:
    proc = _git_run(wt, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _normalized_diff(wt, base: str) -> str | None:
    """The run's diff (base..HEAD, as diff_artifact) with the 'index <blob>..<blob>' lines
    removed, so the same change replayed onto another parent compares equal. None when
    git fails."""
    proc = _git_run(wt, "diff", f"{base}..HEAD")
    if proc.returncode != 0:
        return None
    return "\n".join(l for l in proc.stdout.splitlines() if not l.startswith("index "))


def _wait_clock_advance(after: datetime) -> None:
    """Sleep (at most _CLOCK_ADVANCE_MAX_S) until the UTC clock's whole second is past
    `after`'s whole second: a record written next then sorts strictly after one written
    at `after` even where timestamps are stored at second resolution."""
    target = _as_utc(after).replace(microsecond=0) + timedelta(seconds=1)
    deadline = time.monotonic() + _CLOCK_ADVANCE_MAX_S
    while datetime.now(timezone.utc) < target and time.monotonic() < deadline:
        time.sleep(0.02)


def _grant_audit(act, run: Run) -> None:
    """Carry an audit forward to the run's NEW verified_sha with the same records
    `navigator audited` writes (cli._audited): a run.audited event and an approved
    REVIEW-gate approval by AUDITOR. The gate must be review: that is the gate
    cli.is_audited reads (a merge-gate approval is never recognised)."""
    _emit(act, run, "run.audited", verified_sha=run.verified_sha, actor=AUDITOR,
          carried_forward=True)
    appr = act.create_approval(target=run.id, gate=ApprovalGate.review)
    act.decide_approval(appr.id, ApprovalDecision.approved, actor=AUDITOR)


def _grant_rebuild(act, run: Run) -> bool:
    """One automatic rebuild grant for the run's proposal (the CLI `retry` record, actor
    ACTOR, which Navigator._may_run counts), unless the proposal already holds a grant
    per run: never more than one rebuild per stale failure. True if one was created."""
    runs = act.list_runs_for_proposal(run.proposal_id)
    grants = act.list_approvals(target=run.proposal_id, gate=ApprovalGate.review,
                                decision=ApprovalDecision.approved)
    if len(grants) >= len(runs):
        return False
    appr = act.create_approval(target=run.proposal_id, gate=ApprovalGate.review)
    act.decide_approval(appr.id, ApprovalDecision.approved, actor=ACTOR)
    return True


def _fail_stale(act, orc, run: Run, settings, *, summary: str, lessons: list[str],
                notes: str | None = None) -> None:
    """Fail a sibling that could not be carried over the merge, keep its worktree for
    inspection and schedule exactly one rebuild. Sets run.status first so a caller can
    tell a failure that raised part-way from one that never started."""
    run.status = RunStatus.failed; run.phase = RunPhase.finalizing
    run.ended_at = datetime.now(timezone.utc); _save(act, run)
    _write_outcome(orc, run, status="failed", summary=summary, lessons=lessons,
                   verification_notes=notes, preserved=run.worktree_path, settings=settings)
    _emit(act, run, "run.failed", Severity.error, error=summary)
    granted = _grant_rebuild(act, run)
    _emit(act, run, "run.rebuild_scheduled", granted=granted)


def _stale_skip_reason(repo: str, run: Run) -> str | None:
    """Reason a parked run must be left alone (never rebase what was not verified, or a
    branch/worktree someone touched since verification), else None."""
    if not run.verified_sha:
        return "no verified sha"
    wt = run.worktree_path
    if not wt or not Path(wt).is_dir():
        return "no worktree on disk"
    reason = _verified_check(repo, run)
    if reason:
        return reason
    if _head(wt) != run.verified_sha:
        return "worktree HEAD != verified_sha"
    status = _git_run(wt, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0 or status.stdout.strip():
        return "worktree has uncommitted changes"
    return None


CHECKER_STORE = Path("/Users/Manoj/calwbot/logs/navigator/checker/store.json")


def _frontend_kwargs(settings, proposal) -> dict:
    """run_build_and_verify/reverify kwargs for the proposal's repo: its frontend_dir, the
    live checkout to clone node_modules from and, for a site-checker proposal, the faults
    filed under it (the checker re-check)."""
    cfg = repo_config_for(settings, proposal.repo)
    kw = {"frontend_dir": getattr(cfg, "frontend_dir", None), "repo_path": Path(proposal.repo)}
    if getattr(proposal, "origin", None) == CHECKER_ORIGIN:
        kw["checker_fingerprints"] = FindingStore(CHECKER_STORE).fingerprints_for(proposal.id)
    return kw


def _recorded_argvs(verification) -> list[list[str]]:
    """The commands a run was verified with: argv_list where recorded, else the argv
    string split shell-style (entries recorded before argv_list existed). Entries tagged
    "suite" were run by Navigator itself (frontend suite, checker re-check) and are re-run by
    reverify, not replayed through the allowlist."""
    out = []
    for entry in verification or []:
        if not isinstance(entry, dict) or entry.get("suite"):
            continue
        argv = entry.get("argv_list") or shlex.split(str(entry.get("argv") or ""))
        if argv:
            out.append([str(a) for a in argv])
    return out


def _stale_operator_ok(act, run: Run) -> bool:
    """N12g: True if an OPERATOR's approved merge approval for the run was decided
    before the run's current heartbeat (the _save that re-opened it for review after a
    rebase): that OK no longer counts (_operator_approved ignores it), so the operator
    has to approve the fresh pending request."""
    opened = _as_utc(run.heartbeat_at)
    for appr in _approved_merge_approvals(act, run):
        decided = getattr(appr, "decided_at", None)
        if (appr.actor in _OPERATOR_ACTORS and decided is not None
                and _as_utc(decided) < opened):
            return True
    return False


def _rebase_one(run: Run, merged: Run, new_head: str, *, act, orc, runner, settings,
                workdir: str, state: dict) -> bool:
    """Rebase one parked sibling onto new_head and re-verify it. True if the run is
    parked again (rebased); False if it failed (a rebuild was scheduled)."""
    wt = run.worktree_path
    was_audited = bool(is_audited(act, run))  # judged on the records BEFORE any git
    before = _normalized_diff(wt, run.base_commit_sha)
    rebase = _git_run(wt, *_GIT_FLAGS, "rebase", new_head)
    if rebase.returncode != 0:
        output = rebase.stdout + rebase.stderr
        _git_run(wt, *_GIT_FLAGS, "rebase", "--abort")
        status = _git_run(wt, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0 or status.stdout.strip():
            _log.warning("rebase_stale.worktree_dirty_after_abort", run_id=run.id)
        if "CONFLICT" in output:
            summary = f"stale after merge of {merged.id or 'an earlier merge'}: rebase conflict"
        else:
            first = next((l.strip() for l in output.splitlines() if l.strip()), "")[:200]
            summary = f"stale after merge of {merged.id or 'an earlier merge'}: rebase failed: {first}"
        _fail_stale(act, orc, run, settings, summary=summary, notes=output[-1500:],
                    lessons=["a sibling merged first; rebuilt once against the new HEAD"])
        return False
    state["rebased"] = True
    names = _git_run(wt, "diff", "--name-only", f"{new_head}..HEAD")
    # a git failure stays 'unknown' (None); a genuinely empty name list is refused by
    # reverify (N17: the sibling's change is already in HEAD, the rebase dropped it)
    changed = ([l.strip() for l in names.stdout.splitlines() if l.strip()]
               if names.returncode == 0 else None)
    prop = act.get_proposal(run.proposal_id)
    res = reverify(runner, run_dir_for(Path(wt), workdir), _recorded_argvs(run.verification),
                   changed_files=changed, wt=Path(wt),
                   **(_frontend_kwargs(settings, prop) if prop is not None else {}))
    if res.verify_returncode != 0:
        if res.no_change:
            summary = f"stale after merge of {merged.id or 'an earlier merge'}: {NO_CHANGE_SUMMARY}"
            lessons = [f"{NO_CHANGE_SUMMARY} after rebase: the sibling's change is already in "
                       f"HEAD (merged by {merged.id or 'an earlier merge'}); rebuilt once against the new HEAD"]
        else:
            summary = f"stale after merge of {merged.id or 'an earlier merge'}: re-verify failed"
            lessons = [f"re-verify rc={res.verify_returncode}: {res.verify_output[-300:]}"]
        _fail_stale(act, orc, run, settings, summary=summary, lessons=lessons,
                    notes=res.verify_output[-1500:])
        return False
    old_sha = run.verified_sha
    run.base_commit_sha = new_head
    run.verified_sha = _head(wt) or None
    run.verification = carry_exercise(run.verification, res.verify_log)
    after = _normalized_diff(wt, new_head)
    identical = before is not None and after is not None and before == after
    if identical:
        act.update_run(run)  # what the operator reviewed is unchanged: keep the heartbeat
    else:
        _save(act, run)      # a new diff: earlier merge decisions no longer count
    # N12g (live 2026-10-20): an operator OK decided before the rebase moved the
    # heartbeat is ignored by the merge gate, and the run's only merge request had
    # already been decided, so `navigator merge` found nothing to approve. Open ONE
    # fresh pending request (never a second while one is pending) and say so.
    if not act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.pending):
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    reapproval = _stale_operator_ok(act, run)
    if reapproval:
        _log.info("rebase_stale.reapproval_needed", run_id=run.id)
    prop = act.get_proposal(run.proposal_id)
    try:
        # -z + inside the guard, as in execute_proposal (3a2); the `names`/`changed` lines above feed
        # reverify and stay as they are
        tier_names = _git_run(wt, "diff", "--no-renames", "--name-only", "-z", f"{new_head}..HEAD")
        tier_changed = ([n for n in tier_names.stdout.split("\x00") if n]
                        if tier_names.returncode == 0 else None)
        tier = (classify_for_repo(tier_changed, prop.repo, repo_config_for(settings, prop.repo),
                                  origin=prop.origin)
                if (tier_changed is not None and prop is not None) else None)
    except Exception as e:
        tier = None
        _log.warning("merge_tier.failed", run_id=run.id, error=type(e).__name__)
    _emit(act, run, "run.awaiting_review", diff=diff_artifact(wt, new_head),
          title=prop.title if prop else run.id, verification=run.verification,
          verified_sha=run.verified_sha, rebased_from=old_sha, merged_run_id=merged.id,
          rebased=True, merge_reapproval_needed=reapproval, merge_tier=tier)
    emitted_at = datetime.now(timezone.utc)
    if identical and was_audited:
        _wait_clock_advance(emitted_at)  # the carried audit must sort strictly later
        _grant_audit(act, run)
    else:
        _emit(act, run, "run.rebased_needs_audit",
              reason="diff changed on rebase" if was_audited else "no prior audit")
    return True


def needs_rebase(repo, run) -> bool:
    """The run was verified on an older main than the repo's HEAD (a sibling merged since)."""
    head = _rev_parse(repo, "HEAD")
    return bool(head) and getattr(run, "base_commit_sha", None) != head


def rebase_stale_runs(merged: Run | None, *, act, orc, runner, settings, repo: str,
                      workdir: str = ".", exclude_ids=(), limit: int | None = None,
                      only_ids=None, stats: dict | None = None) -> list[Run]:
    """After `merged` landed in `repo`: rebase every other run of the repo parked at
    the merge gate onto the new HEAD and re-verify it (no model call). Returns the runs
    that failed (conflict, re-verify failure or bookkeeping error after a successful
    rebase); each of those got exactly one rebuild grant. A sibling whose _fail_stale
    raised part-way is still returned as failed with its single rebuild grant. Runs
    whose branch or worktree moved since verification are left untouched
    (run.rebase_skipped)."""
    failed: list[Run] = []
    try:
        new_head = _rev_parse(repo, "HEAD")
        candidates = list(act.list_open_runs())
    except (OSError, subprocess.SubprocessError, TypeError) as e:
        _log.warning("rebase_stale.unavailable", error=type(e).__name__)
        return failed
    if not new_head:
        _log.warning("rebase_stale.no_head", repo=str(repo))
        return failed
    # 2026-11-02: at most `limit` re-verifies per call (each one runs a suite); the rest wait for
    # the next tick. A run already on HEAD is never re-verified again.
    if merged is None:
        from types import SimpleNamespace
        merged = SimpleNamespace(id=None)
    skip = set(exclude_ids) | ({merged.id} if merged.id else set())
    done = 0
    for run in candidates:
        if run.id in skip or run.phase != RunPhase.awaiting_review:
            continue
        if only_ids is not None and run.id not in only_ids:
            continue
        if run.base_commit_sha == new_head:
            continue
        if limit is not None and done >= limit:
            break
        prop = act.get_proposal(run.proposal_id)
        if prop is None or prop.repo != repo:
            continue
        state = {"rebased": False}
        try:
            reason = _stale_skip_reason(repo, run)
            if reason:
                if merged.id:  # once, right after the merge; a catch-up tick doesn't repeat it
                    _emit(act, run, "run.rebase_skipped", Severity.warning, reason=reason,
                          merged_run_id=merged.id)
                continue
            done += 1
            if stats is not None:
                stats["attempted"] = stats.get("attempted", 0) + 1
            if not _rebase_one(run, merged, new_head, act=act, orc=orc, runner=runner,
                               settings=settings, workdir=workdir, state=state):
                failed.append(run)
        except Exception as e:
            # class name only: a message may carry paths or command output
            _log.warning("rebase_stale.run_failed", run_id=run.id, error=type(e).__name__)
            try:
                if run.status == RunStatus.failed:
                    # _fail_stale itself raised part-way (ACT/ORC write): the rebase was
                    # already aborted or the worktree kept; finish the idempotent
                    # bookkeeping so the run is never failed without its one rebuild.
                    # _grant_rebuild counts existing grants, so this cannot add a second
                    # one when the grant had already been created before the raise. The
                    # outcome note / run.failed event that were not reached are NOT
                    # retried (an ACT/ORC that just failed must not be hammered).
                    _save(act, run)
                    _emit(act, run, "run.rebuild_scheduled", granted=_grant_rebuild(act, run),
                          recovered_from=type(e).__name__)
                    failed.append(run)
                elif state["rebased"]:
                    _fail_stale(act, orc, run, settings,
                                summary=f"stale after merge of {merged.id or 'an earlier merge'}: rebase bookkeeping failed",
                                lessons=[f"rebase bookkeeping raised {type(e).__name__}"])
                    failed.append(run)
                else:
                    _emit(act, run, "run.rebase_skipped", Severity.warning,
                          reason=f"error: {type(e).__name__}", merged_run_id=merged.id)
            except Exception as e2:
                _log.warning("rebase_stale.cleanup_failed", run_id=run.id,
                             error=type(e2).__name__)
            continue
    return failed
