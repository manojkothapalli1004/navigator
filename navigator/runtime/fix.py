"""The Fix action (B4, design 2026-11-04): one rebuild for a declined run, carrying its reason.

A run the operator declined at the merge gate is `aborted`; its proposal stays `approved`
but loop._may_run lets a proposal run again only while len(runs) <= len(approved
review-gate grants targeting the proposal). After one decline that count is runs=1 /
grants=0, so the proposal is parked. Until B4 only `cli._retry` and
executor._grant_rebuild wrote such a grant; neither was reachable from Telegram or the ACT
Decisions page. This module writes exactly that record on request - the same approval
cli retry writes - with the note 'fix <run-id>' (B3's hook for which decline reason the
rebuild carries) and a run.fix_requested event. It never re-approves a proposal (unlike
_retry) and never writes a second grant while one is unused: fix_reason refuses when
effective_grants(grants) >= len(runs), the _grant_rebuild rule, or when a grant noted for
the same declined run already exists.

B4d: a UI tap and a Telegram tap within a second both pass fix_reason and write two
grants for one decline. Those are ONE effective grant: effective_grants counts the grants
without a 'fix ' note (cli retry, _grant_rebuild) each and the 'fix <run-id>' notes once
per DISTINCT run id, and loop._may_run, executor._grant_rebuild and fix_reason all count
that way - so the spare grant never pays for an unattended second rebuild.

Imports only contracts and the logger: telegram_inbox and decisions import this module, so
it must never import them (or cli / executor / loop) back. The proposal short name is
derived here (first title token without a trailing ':', as telegram_inbox.short_name).
"""
from datetime import datetime, timedelta, timezone

from navigator.contracts.approval import Approval
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, ProposalStatus, RunStatus
from navigator.contracts.event import Event
from navigator.observability.logging import get_logger

log = get_logger("navigator.fix")

# declines older than this are not offered (the worktree may be gone, the base moved on)
WINDOW_DAYS = 7
MAX_CANDIDATES = 10
# fixable_candidates looks at this many declines (newest first) for MAX_CANDIDATES fixable
SCAN_LIMIT = 50
# the grant's note: FIX_NOTE + run id of the declined run whose reason the rebuild carries
FIX_NOTE = "fix "
EVENT_KIND = "run.fix_requested"
_REASON_PREFIX = "reason="


def short_name(title) -> str:
    """First token of a proposal title with a trailing ':' removed: 'N5b: ...' -> 'N5b'."""
    parts = (title or "").split()
    return parts[0].rstrip(":") if parts else ""


def _aware(dt) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _floor() -> datetime:
    return datetime.min.replace(tzinfo=timezone.utc)


def effective_grants(approvals) -> int:
    """How many rebuilds a list of approved review grants buys: every grant whose note
    does not start with FIX_NOTE (cli retry, _grant_rebuild: no note) counts, and the
    notes that do count once per DISTINCT note - two Fix grants both noted 'fix run1'
    are one. Pure; a missing or None note is a plain grant."""
    plain = 0
    fixes: set[str] = set()
    for a in approvals:
        note = getattr(a, "note", None)
        if isinstance(note, str) and note.startswith(FIX_NOTE):
            fixes.add(note)
        else:
            plain += 1
    return plain + len(fixes)


def declined_candidates(act, now: datetime | None = None,
                        limit: int | None = MAX_CANDIDATES) -> list[tuple]:
    """Runs the operator may fix, newest decline first, at most `limit` (None: all): the newest
    run of its proposal OVERALL (a proposal already rebuilding or rebuilt is never offered),
    status aborted, ended within WINDOW_DAYS, with a declined merge approval (an N16 quota
    deferral is aborted too but has none). Each entry is (run, short name, title, proposal).
    The proposal's status is NOT filtered here so fix_reason can say 'proposal is declined'.
    A proposal that cannot be fetched (vanished, ACT hiccup) drops only its own run."""
    now = _aware(now) or datetime.now(timezone.utc)
    floor = now - timedelta(days=WINDOW_DAYS)
    newest: dict = {}
    for run in act.list_all_runs():
        cur = newest.get(run.proposal_id)
        if cur is None or (_aware(run.started_at) or _floor()) > (_aware(cur.started_at) or _floor()):
            newest[run.proposal_id] = run
    declined = []
    for run in newest.values():
        if run.status != RunStatus.aborted:
            continue
        ended = _aware(run.ended_at)
        if ended is None or ended < floor:
            continue
        if not act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.declined):
            continue
        declined.append((ended, run))
    declined.sort(key=lambda pair: pair[0], reverse=True)
    out = []
    for _ended, run in declined:
        try:
            prop = act.get_proposal(run.proposal_id)
        except Exception as e:
            log.warning("fix.candidate_proposal_failed", run_id=run.id, error=type(e).__name__)
            continue
        if prop is None:
            continue
        title = (prop.title or "").strip()
        out.append((run, short_name(title) or run.id, title or run.id, prop))
        if limit is not None and len(out) >= limit:
            break
    return out


def fixable_candidates(act, now: datetime | None = None, declined=None) -> list[tuple]:
    """declined_candidates filtered by fix_reason(..., run=run) is None and THEN capped at
    MAX_CANDIDATES, scanning at most SCAN_LIMIT declines: when the newest declines are all
    queued or rebuilding, an older fixable one is still offered. One fix_reason (one
    list_runs_for_proposal, one list_approvals) per decline scanned. `declined`: a
    declined_candidates list the caller already holds (decisions passes the uncapped one it
    counts name collisions over); only its first SCAN_LIMIT are scanned."""
    if declined is None:
        declined = declined_candidates(act, now, limit=SCAN_LIMIT)
    out = []
    for run, short, title, prop in declined[:SCAN_LIMIT]:
        if fix_reason(act, prop, act.list_runs_for_proposal(run.proposal_id), run=run) is None:
            out.append((run, short, title, prop))
            if len(out) >= MAX_CANDIDATES:
                break
    return out


def fix_reason(act, proposal, runs, run=None) -> str | None:
    """Why a fix is refused right now, or None when one grant may be written. Mirrors
    cli._retry (a proposal with an executing or completed run has nothing to retry) and
    executor._grant_rebuild (refuse when effective_grants(approved review grants) >=
    len(runs): every extra run already has its grant, so a second Fix would queue a second
    rebuild). With `run` (the declined run) a grant noted 'fix <run.id>' refuses too, even
    while the count allows: the same decline is never fixed twice (a new decline has a new
    run id and may be)."""
    if proposal is None:
        return "proposal not found"
    if proposal.status != ProposalStatus.approved:
        return f"proposal is {proposal.status.value}"
    if any(r.status == RunStatus.executing for r in runs):
        return "a build is running"
    if any(r.status == RunStatus.completed for r in runs):
        return "already merged"
    grants = act.list_approvals(target=proposal.id, gate=ApprovalGate.review,
                                decision=ApprovalDecision.approved)
    if effective_grants(grants) >= len(runs):
        return "already queued"
    if run is not None and any(getattr(a, "note", None) == f"{FIX_NOTE}{run.id}"
                               for a in grants):
        return "already queued"
    return None


def decline_note(act, run_id: str) -> str | None:
    """The note of the run's declined merge approval: the first one starting with
    'reason=' (the one executor._decline_reason reads the code from), else the first
    declined approval with any note; None when there is none."""
    first = None
    for a in act.list_approvals(target=run_id, gate=ApprovalGate.merge,
                                decision=ApprovalDecision.declined):
        note = getattr(a, "note", None)
        if not isinstance(note, str) or not note:
            continue
        if note.startswith(_REASON_PREFIX):
            return note
        if first is None:
            first = note
    return first


def request_fix(act, run, actor: str, update_id: int = 0) -> Approval:
    """Write one rebuild grant for the run's proposal: an approved review-gate approval
    targeting the proposal (what _may_run counts) with the note 'fix <run-id>', then a
    run.fix_requested event. The grant stands once decide_approval returns: a failing event
    log is logged by class name only and never raises, so no caller tells the operator to
    re-send (that would write a second grant). Call fix_reason first; this never checks."""
    appr = act.create_approval(target=run.proposal_id, gate=ApprovalGate.review)
    act.decide_approval(appr.id, ApprovalDecision.approved, actor=actor,
                        note=f"{FIX_NOTE}{run.id}")
    try:
        act.emit_event(Event(kind=EVENT_KIND, correlation_id=run.correlation_id,
                             payload={"run_id": run.id, "proposal_id": run.proposal_id,
                                      "actor": actor, "update_id": update_id}))
    except Exception as e:
        log.warning("fix.event_failed", run_id=run.id, error=type(e).__name__)
    return appr
