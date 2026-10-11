"""Operator commands: `python -m navigator <command> [id]`.

Every decision here is recorded in ACT; the daemon acts on it next cycle.
"""
import subprocess
from typing import TextIO
from navigator.observability.notify import format_verification
from navigator.contracts.enums import (ApprovalDecision, ApprovalGate, ProposalStatus,
                                       RunPhase, RunStatus)

COMMANDS = ("status", "approve", "decline", "diff", "merge", "reject", "retry")
ACTOR = "operator"


def run_cli(argv: list[str], *, act, out: TextIO) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: navigator {{{'|'.join(COMMANDS)}}} [id]", file=out)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        return _status(act, out)
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


_HANDLERS = {
    "approve": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.approved),
    "decline": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.declined),
    "merge": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.approved),
    "reject": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.declined),
    "diff": _diff,
    "retry": _retry,
}
