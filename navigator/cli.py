"""Operator commands: `python -m navigator <command> [id]`.

Every decision here is recorded in ACT; the daemon acts on it next cycle.
`cleanup [--apply]` is the exception: it removes finished runs' worktrees and
nav/run-* branches directly (dry run unless --apply).
"""
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO
from navigator.observability.notify import format_verification
from navigator.contracts.enums import (ApprovalDecision, ApprovalGate, ProposalStatus,
                                       RunPhase, RunStatus)

COMMANDS = ("status", "approve", "decline", "diff", "merge", "reject", "retry", "cleanup")
ACTOR = "operator"

# run statuses whose worktree/branch can go regardless of age
_FINISHED_STATUSES = {"completed", "declined", "aborted"}
# failed runs are kept this long for inspection
_FAILED_GRACE = timedelta(days=7)
_BRANCH_PREFIX = "nav/run-"
_ALWAYS_PROTECTED = {"main", "master"}


def run_cli(argv: list[str], *, act, out: TextIO, worktree_root: str | None = None) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: navigator {{{'|'.join(COMMANDS)}}} [id]  "
              f"(cleanup takes no id: navigator cleanup [--apply])", file=out)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        return _status(act, out)
    if cmd == "cleanup":
        if args not in ([], ["--apply"]):
            print("usage: navigator cleanup [--apply]", file=out)
            return 2
        return _cleanup(act, apply=bool(args), out=out, worktree_root=worktree_root)
    if len(args) != 1:
        print(f"navigator {cmd} needs exactly one id", file=out)
        return 2
    return _HANDLERS[cmd](act, args[0], out)


def _status(act, out) -> int:
    proposed = act.list_proposals(ProposalStatus.proposed)
    approved = act.list_proposals(ProposalStatus.approved)
    print(f"proposed ({len(proposed)}):", file=out)
    for p in proposed:
        print(f"  {p.id}  {p.title}  [{', '.join(p.affected_files)}]", file=out)
    print(f"approved ({len(approved)}):", file=out)
    for p in approved:
        runs = act.list_runs_for_proposal(p.id)
        state = ", ".join(f"{r.id}:{r.status.value}" for r in runs) or "no run yet"
        print(f"  {p.id}  {p.title}  ({state})", file=out)
    runs = act.list_open_runs()
    print(f"open runs ({len(runs)}):", file=out)
    for r in runs:
        print(f"  {r.id}  {r.phase.value}  branch={r.branch}", file=out)
    return 0


def _set_proposal(act, pid, out, status: ProposalStatus) -> int:
    p = act.get_proposal(pid)
    if p is None:
        print(f"no such proposal: {pid}", file=out)
        return 1
    if p.status != ProposalStatus.proposed:
        print(f"{pid} is {p.status.value}, not proposed", file=out)
        return 1
    act.set_proposal_status(pid, status)
    print(f"{pid} -> {status.value}", file=out)
    return 0


def _decide_merge(act, run_id, out, decision: ApprovalDecision) -> int:
    pending = act.list_approvals(target=run_id, gate=ApprovalGate.merge,
                                 decision=ApprovalDecision.pending)
    if not pending:
        print(f"no pending merge approval for {run_id}", file=out)
        return 1
    act.decide_approval(pending[0].id, decision, actor=ACTOR)
    print(f"{run_id} merge -> {decision.value} (applied on the daemon's next cycle)", file=out)
    return 0


def _find_run(act, run_id):
    return next((r for r in act.list_open_runs() if r.id == run_id), None)


def _diff(act, run_id, out) -> int:
    run = _find_run(act, run_id)
    prop = act.get_proposal(run.proposal_id) if run else None
    if run is None or prop is None:
        print(f"no open run: {run_id}", file=out)
        return 1
    print(f"{run.id}  {prop.title}  (model calls: {run.model_calls})", file=out)
    print(format_verification(run.verification), file=out)
    d = subprocess.run(["git", "diff", f"{run.base_commit_sha}..{run.branch}"], cwd=prop.repo,
                       capture_output=True, text=True)
    out.write(d.stdout or d.stderr)
    return d.returncode


def _retry(act, pid, out) -> int:
    p = act.get_proposal(pid)
    if p is None:
        print(f"no such proposal: {pid}", file=out)
        return 1
    runs = act.list_runs_for_proposal(pid)
    if not runs or any(r.status in (RunStatus.executing, RunStatus.completed) for r in runs):
        print(f"{pid} has no failed run to retry (or one is open/completed)", file=out)
        return 1
    if p.status != ProposalStatus.approved:
        act.set_proposal_status(pid, ProposalStatus.approved)
    grant = act.create_approval(target=pid, gate=ApprovalGate.review)
    act.decide_approval(grant.id, ApprovalDecision.approved, actor=ACTOR)
    print(f"{pid}: one retry granted (runs on the daemon's next execute cycle)", file=out)
    return 0


# ---- cleanup -----------------------------------------------------------------

def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _resolve_worktree_root(worktree_root: str | None) -> str | None:
    if worktree_root:
        return worktree_root
    env = os.environ.get("NAVIGATOR_WORKTREE_ROOT")
    if env:
        return env
    try:
        from navigator.config import Settings
        return Settings().worktree_root or None
    except Exception:
        return None


def _eligible(run, now: datetime) -> bool:
    status = run.status.value
    if status in _FINISHED_STATUSES:
        return True
    if status == "failed" and run.ended_at is not None:
        ended = run.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        return now - ended > _FAILED_GRACE
    return False


def _protected_branches(repo) -> set[str]:
    protected = set(_ALWAYS_PROTECTED)
    cur = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if cur.returncode == 0 and cur.stdout.strip():
        protected.add(cur.stdout.strip())
    return protected


def _cleanup(act, *, apply: bool, out, worktree_root: str | None = None,
             now: datetime | None = None) -> int:
    root_s = _resolve_worktree_root(worktree_root)
    if not root_s:
        print("cleanup needs NAVIGATOR_WORKTREE_ROOT to be set", file=out)
        return 1
    root = Path(root_s).expanduser().resolve()
    now = now or datetime.now(timezone.utc)
    prefix = "" if apply else "would "
    touched: list[str] = []
    protected_by_repo: dict[str, set[str]] = {}
    failures = 0

    for run in act.list_all_runs():
        if run.status == RunStatus.executing or not _eligible(run, now):
            continue
        prop = act.get_proposal(run.proposal_id)
        if prop is None or not prop.repo or not Path(prop.repo).is_dir():
            print(f"skip {run.id}: repo unknown or missing", file=out)
            continue
        repo = prop.repo
        acted = False

        wt = Path(run.worktree_path) if run.worktree_path else root / run.id
        wt = wt.expanduser().resolve()
        if wt.exists() and root in wt.parents:
            acted = True
            print(f"{prefix}remove worktree {wt} ({run.id})", file=out)
            if apply:
                r = _git(repo, "worktree", "remove", "--force", str(wt))
                if r.returncode != 0:
                    failures += 1
                    print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)

        branch = f"nav/{run.id}"
        if repo not in protected_by_repo:
            protected_by_repo[repo] = _protected_branches(repo)
        if (branch.startswith(_BRANCH_PREFIX) and branch not in protected_by_repo[repo]
                and _git(repo, "rev-parse", "--verify", "--quiet",
                         f"refs/heads/{branch}").returncode == 0):
            acted = True
            print(f"{prefix}delete branch {branch} in {repo} ({run.id})", file=out)
            if apply:
                r = _git(repo, "branch", "-D", branch)
                if r.returncode != 0:
                    failures += 1
                    print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)

        if acted and repo not in touched:
            touched.append(repo)

    for repo in touched:
        print(f"{prefix}prune worktrees in {repo}", file=out)
        if apply:
            r = _git(repo, "worktree", "prune")
            if r.returncode != 0:
                failures += 1
                print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)
    if not touched:
        print("nothing to clean up", file=out)
    return 1 if failures else 0


_HANDLERS = {
    "approve": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.approved),
    "decline": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.declined),
    "merge": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.approved),
    "reject": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.declined),
    "diff": _diff,
    "retry": _retry,
}
