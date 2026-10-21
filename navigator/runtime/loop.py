import json
import re
import subprocess
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import (ProposalStatus, RunStatus, RunPhase, ApprovalGate,
                                       ApprovalDecision)
from navigator.runtime.executor import (execute_proposal, merge_after_approval, decline_run,
                                        rebase_stale_runs)
from navigator.runtime.lock import SingleFlightLock
from navigator.runtime.outcome_store import OutcomeStore, title_lessons
from navigator.observability.logging import get_logger
from navigator.observability.notify import NullNotifier
from navigator.safety.errors import QuotaExhaustedError

log = get_logger("navigator.loop")

# N16: how long execution pauses after a QuotaExhaustedError that names no reset time
QUOTA_DEFAULT_PAUSE_S = 3600

class Navigator:
    def __init__(self, *, act, orc, driver, runner, settings, repo: str, lock_path,
                 workdir: str = ".", squash: bool = False, brief: str | None = None,
                 context_files: list[str] | None = None,
                 min_plan_interval_hours: float | None = None,
                 max_proposals_per_tick: int | None = None):
        self.act = act; self.orc = orc; self.driver = driver; self.runner = runner
        self.settings = settings; self.repo = repo
        self.workdir = workdir  # where verifier commands run inside the repo
        self.squash = squash    # public repos: single squash commit, no tool identities
        # per-repo planner shaping (e.g. trader research loop: one experiment a day)
        self.brief = brief
        self.context_files = context_files or []
        self.min_plan_interval_hours = min_plan_interval_hours
        self.max_proposals_per_tick = max_proposals_per_tick
        self.lock = SingleFlightLock(lock_path)
        self.notifier = NullNotifier()
        self.last_snapshot_chars = 0
        # N16: set when a build was deferred on an account-wide limit; approval_poll
        # executes nothing until quota_paused_until. quota_deferral describes the
        # deferral of the LAST poll only (class name + resets_at, never SDK text).
        self.quota_paused_until: datetime | None = None
        self.quota_deferral: dict | None = None

    def perceive(self, base_commit_sha: str, open_proposals: list[Proposal] | None = None) -> dict:
        if open_proposals is None:
            open_proposals = self.act.list_proposals(ProposalStatus.proposed)
        git_state = subprocess.run(["git","status","--porcelain"], cwd=self.repo,
                                   capture_output=True, text=True).stdout
        # recent commits let the planner see what already shipped; without them it
        # kept proposing finished work (outcome store, timestamps) for weeks
        recent = subprocess.run(["git","log","--oneline","-15"], cwd=self.repo,
                                capture_output=True, text=True).stdout.splitlines()
        # real paths keep proposals' affected_files accurate — the builder only
        # sees the files a proposal names
        files = subprocess.run(["git","ls-files"], cwd=self.repo,
                               capture_output=True, text=True).stdout.splitlines()[:400]
        outcomes = OutcomeStore(self.settings.outcome_path).latest(5)
        snap = {
            "orc_memory": _trim_memory(self.orc.get_canonical_memory(),
                                       self.settings.memory_chars_per_section, Path(self.repo).name),
            # declined lessons name the proposal title so the planner can see what
            # was declined and not re-file near-duplicates of it
            "recent_outcomes": [{"proposal_id": o.proposal_id, "status": o.status,
                                 "summary": o.summary,
                                 "lessons": title_lessons(
                                     o.lessons, self._proposal_title(o.proposal_id))}
                                for o in outcomes],
            "act": {"open_proposals": [{"id": p.id, "title": p.title} for p in open_proposals]},
            # self-learning: propose more of what the operator approves, less of what
            # they decline or triage retired
            "operator_decisions": self._recent_decisions(),
            "git": {"base_commit_sha": base_commit_sha, "dirty": bool(git_state.strip()),
                    "recent_commits": recent, "files": files},
        }
        if self.brief:
            snap["repo_brief"] = self.brief
        if self.context_files:
            snap["context_files"] = {rel: self._read_context(rel) for rel in self.context_files}
        return snap

    _CONTEXT_CHARS = 6000

    def _proposal_title(self, proposal_id: str | None) -> str | None:
        if not proposal_id:
            return None
        try:
            p = self.act.get_proposal(proposal_id)
        except Exception:  # ACT lookup failures must never break perceive
            return None
        return p.title if p is not None else None

    def _recent_decisions(self, n: int = 20) -> list[dict]:
        decided = [p for p in self._mine(self.act.list_proposals())
                   if p.status != ProposalStatus.proposed]
        decided.sort(key=lambda p: p.created_at if p.created_at.tzinfo
                     else p.created_at.replace(tzinfo=timezone.utc), reverse=True)
        return [{"title": p.title[:120], "decision": p.status.value} for p in decided[:n]]

    def _read_context(self, rel: str) -> str | None:
        # read from the live checkout: research outputs are gitignored, so they never
        # exist in a run worktree; tail-capped so one file can't bloat the prompt
        root = Path(self.repo).resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target.read_text(errors="replace")[-self._CONTEXT_CHARS:]

    def _planned_recently(self) -> bool:
        if not self.min_plan_interval_hours:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.min_plan_interval_hours)
        for p in self._mine(self.act.list_proposals()):
            created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                return True
        return False

    def _expire_stale(self, proposals: list[Proposal]) -> list[Proposal]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.proposal_ttl_days)
        live = []
        for p in proposals:
            created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc)
            if created < cutoff:
                self.act.set_proposal_status(p.id, ProposalStatus.expired)
            else:
                live.append(p)
        return live

    def _mine(self, proposals: list[Proposal]) -> list[Proposal]:
        # several Navigators (one per repo) share one ACT; each owns only its repo's work
        return [p for p in proposals if p.repo == self.repo]

    def periodic_tick(self, base_commit_sha: str | None = None) -> list[Proposal]:
        if base_commit_sha is None:
            base_commit_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                             capture_output=True, text=True).stdout.strip()
        # unreviewed proposals expire so the backlog cap can't idle the daemon forever
        open_proposals = self._expire_stale(
            self._mine(self.act.list_proposals(ProposalStatus.proposed)))
        if len(open_proposals) >= self.settings.max_open_proposals:
            log.info("tick.backlog_full", open=len(open_proposals))
            return []
        if self._planned_recently():
            return []
        snap = self.perceive(base_commit_sha, open_proposals)
        self.last_snapshot_chars = len(json.dumps(snap, default=str))
        from navigator.runtime.drivers.base import DriverRequest
        from navigator.contracts.enums import Role
        plan = self.driver.run_role(DriverRequest(role=Role.planner, context={"repo": self.repo, "snapshot": snap}))
        created: list[Proposal] = []
        all_proposals = self._mine(self.act.list_proposals())
        existing_hashes = {p.proposal_origin_hash for p in all_proposals}
        existing_titles = {_norm_title(p.title) for p in all_proposals}
        # reworded duplicates slip past exact-title dedup (live 2026-10-12: 3-4 variants
        # of the same idea); compare content words against every prior title, any status
        seen_titles: list[str] = [p.title for p in all_proposals]
        room = self.settings.max_open_proposals - len(open_proposals)
        per_tick = self.max_proposals_per_tick or self.settings.max_proposals_per_tick
        for item in plan.proposals[: min(per_tick, room)]:
            h = compute_origin_hash(self.repo, item["intended_changes"], item["affected_files"], "periodic", None)
            if h in existing_hashes or _norm_title(item["title"]) in existing_titles:
                continue
            if _near_duplicate(item["title"], seen_titles):
                log.info("tick.near_duplicate_skipped", title=item["title"])
                continue
            existing_titles.add(_norm_title(item["title"]))
            seen_titles.append(item["title"])
            p = Proposal(origin="periodic", proposal_origin_hash=h, title=item["title"],
                         rationale=item["rationale"], repo=self.repo, base_commit_sha=base_commit_sha,
                         intended_changes=item["intended_changes"], affected_files=item["affected_files"])
            # the backend may mint its own id — the stored proposal is the identity
            created.append(self.act.create_proposal(p))
        return created  # dry_run stops here: no worktree/git mutation

    def _quota_pause_from(self, e: QuotaExhaustedError) -> datetime:
        """When execution may resume after `e`: its resets_at, else the default pause."""
        if e.resets_at:
            try:
                until = datetime.fromisoformat(e.resets_at)
            except ValueError:
                until = None
            if until is not None:
                return until if until.tzinfo is not None else until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) + timedelta(seconds=QUOTA_DEFAULT_PAUSE_S)

    def approval_poll(self) -> list:
        self.quota_deferral = None
        if self.settings.mode != "execute":
            return []
        runs = []
        with self.lock.acquire():
            if (self.quota_paused_until is not None
                    and datetime.now(timezone.utc) < self.quota_paused_until):
                # N16: the account limit has not lifted yet: no build, no SDK call
                log.info("approval_poll.quota_paused",
                         until=self.quota_paused_until.isoformat(timespec="seconds"))
                return runs
            self.quota_paused_until = None
            for p in self._mine(self.act.list_proposals(status=ProposalStatus.approved)):
                # Correction 3: dedup guard — skip proposals that already have a run
                # (any status: a completed run must also block re-execution)
                if not self._may_run(p.id):
                    continue
                try:
                    run = execute_proposal(p, act=self.act, orc=self.orc, driver=self.driver,
                                           runner=self.runner, settings=self.settings,
                                           workdir=self.workdir)
                except QuotaExhaustedError as e:
                    # N16: the run was deferred (aborted + one rebuild grant); every later
                    # proposal would fail the same way, so pause until the limit lifts.
                    # Class name and resets_at only: str(e) may carry a token-bearing URL.
                    self.quota_paused_until = self._quota_pause_from(e)
                    self.quota_deferral = {"error": type(e).__name__, "resets_at": e.resets_at}
                    log.warning("approval_poll.quota_exhausted", error=type(e).__name__,
                                resets_at=e.resets_at)
                    break
                # Correction 3: proposal stays approved; the dedup guard above prevents re-execution
                runs.append(run)
        return runs

    def _may_run(self, proposal_id: str) -> bool:
        # one run per proposal; each operator `retry` grant buys exactly one more
        runs = self.act.list_runs_for_proposal(proposal_id)
        if not runs:
            return True
        if any(r.status in (RunStatus.executing, RunStatus.completed) for r in runs):
            return False
        grants = self.act.list_approvals(target=proposal_id, gate=ApprovalGate.review,
                                         decision=ApprovalDecision.approved)
        return len(runs) <= len(grants)

    def _merges_enabled(self) -> bool:
        # N8: the operator may opt in to acting on merge decisions while the daemon
        # stays in dry_run (NAVIGATOR_MERGE_IN_DRY_RUN=1); builds (approval_poll) stay off
        s = self.settings
        return s.mode == "execute" or (s.mode == "dry_run"
                                       and bool(getattr(s, "merge_in_dry_run", False)))

    def merge_poll(self) -> list:
        """Act on operator decisions at the merge gate; returns runs that finished.
        Runs in execute mode, or in dry_run when settings.merge_in_dry_run is set."""
        if not self._merges_enabled():
            return []
        done = []
        with ExitStack() as stack:
            if not self._enter_lock(stack):
                # a build in another process holds the lock; decisions stay recorded in
                # ACT and are acted on next cycle (live 2026-10-17: every cycle errored)
                log.info("merge_poll.deferred_lock_held", lock=str(self.lock.path))
                return []
            seen: set[str] = set()
            pending = list(self.act.list_open_runs())
            while pending:
                run = pending.pop(0)
                if run.id in seen:
                    continue
                seen.add(run.id)
                if run.phase != RunPhase.awaiting_review:
                    continue
                prop = self.act.get_proposal(run.proposal_id)
                if prop is None or prop.repo != self.repo:
                    continue
                decisions = {a.decision for a in
                             self.act.list_approvals(target=run.id, gate=ApprovalGate.merge)}
                if ApprovalDecision.declined in decisions:
                    done.append(decline_run(run, act=self.act, orc=self.orc, settings=self.settings))
                elif ApprovalDecision.approved in decisions:
                    run = merge_after_approval(run, act=self.act, orc=self.orc, driver=self.driver,
                                               runner=self.runner, settings=self.settings,
                                               squash=self.squash)
                    if run.status != RunStatus.executing:
                        done.append(run)
                    if (run.status == RunStatus.completed
                            and getattr(self.settings, "rebase_stale_runs", True)):
                        # N12d: the repo's other parked runs are now stale; rebase and
                        # re-verify them (no model call) while this process holds the lock
                        failed = rebase_stale_runs(run, act=self.act, orc=self.orc,
                                                   runner=self.runner, settings=self.settings,
                                                   repo=self.repo, workdir=self.workdir,
                                                   exclude_ids={r.id for r in done})
                        done.extend(failed)
                        seen.update(r.id for r in failed)
                        log.info("merge_poll.rebased_stale", merged=run.id,
                                 failed=[r.id for r in failed])
                        # the rebase changed the runs' recorded state: re-read the rest
                        pending = [r for r in self.act.list_open_runs() if r.id not in seen]
        return done

    def _enter_lock(self, stack: ExitStack) -> bool:
        """Enter the single-flight lock on `stack`. False if a live process holds it
        (acquire() already reclaims locks left by dead holders); other errors propagate."""
        try:
            stack.enter_context(self.lock.acquire())
        except RuntimeError as e:
            if str(e).startswith("single-flight lock held"):
                return False
            raise
        return True

    def reconcile_on_startup(self) -> None:
        with ExitStack() as stack:
            if not self._enter_lock(stack):
                # N10: a live process (e.g. a supervised once.sh build) owns the in-flight
                # runs; aborting them here threw away a healthy build on 2026-10-17
                log.warning("reconcile.skipped_lock_held", lock=str(self.lock.path))
                return
            for run in self.act.list_open_runs():
                if run.phase == RunPhase.awaiting_review:
                    continue  # parked at the merge gate by design; merge_poll owns it
                run.status = RunStatus.aborted
                run.phase = RunPhase.finalizing
                run.ended_at = datetime.now(timezone.utc)
                self.act.update_run(run)
                # Record a post-mortem lesson; the worktree (if any) is left on disk for inspection.
                self.orc.apply_memory_update(
                    section="known_failures",
                    note=f"[{run.id}] aborted stale run on startup reconcile | worktree: {run.worktree_path}",
                    replace=False)
                log.warning("reconcile.aborted_stale_run", run_id=run.id,
                            preserved_worktree=run.worktree_path)


_ENTRY_SPLIT = re.compile(r"\n+(?=\[\d{4}-\d{2}-\d{2})")

def _trim_memory(memory: dict, budget: int, focus: str) -> dict:
    """Cap each ORC memory section at `budget` chars. Whole dated entries are kept,
    entries mentioning `focus` (the target repo) first, then newest first."""
    sections = memory.get("sections", memory)
    trimmed = {}
    for name, text in sections.items():
        if not isinstance(text, str):
            continue
        if len(text) <= budget:
            trimmed[name] = text
            continue
        entries = _ENTRY_SPLIT.split(text)
        order = sorted(range(len(entries)),
                       key=lambda i: (focus.lower() not in entries[i].lower(), -i))
        keep, used = set(), 0
        for i in order:
            if used + len(entries[i]) + 2 <= budget:
                keep.add(i); used += len(entries[i]) + 2
        trimmed[name] = ("\n\n".join(entries[i] for i in sorted(keep))
                         if keep else text[-budget:])
    return trimmed

def _norm_title(title: str) -> str:
    return " ".join(title.lower().strip(" .").split())

_STOPWORDS = frozenset("a an the and or for to of in on with add so that every all across".split())
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NEAR_DUP_THRESHOLD = 0.6

def _content_words(title: str) -> set[str]:
    return {w for w in _NON_ALNUM.split(title.lower()) if w and w not in _STOPWORDS}

def _near_duplicate(title: str, titles) -> bool:
    """True if `title` shares >= 0.6 Jaccard similarity of content words with any of `titles`."""
    words = _content_words(title)
    if not words:
        return False
    for other in titles:
        other_words = _content_words(other)
        if not other_words:
            continue
        if len(words & other_words) / len(words | other_words) >= _NEAR_DUP_THRESHOLD:
            return True
    return False
