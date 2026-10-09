import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import (ProposalStatus, RunStatus, RunPhase, ApprovalGate,
                                       ApprovalDecision)
from navigator.runtime.executor import execute_proposal, merge_after_approval, decline_run
from navigator.runtime.lock import SingleFlightLock
from navigator.runtime.outcome_store import OutcomeStore
from navigator.observability.logging import get_logger
from navigator.observability.notify import NullNotifier

log = get_logger("navigator.loop")

class Navigator:
    def __init__(self, *, act, orc, driver, runner, settings, repo: str, lock_path):
        self.act = act; self.orc = orc; self.driver = driver; self.runner = runner
        self.settings = settings; self.repo = repo
        self.lock = SingleFlightLock(lock_path)
        self.notifier = NullNotifier()
        self.last_snapshot_chars = 0

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
        return {
            "orc_memory": _trim_memory(self.orc.get_canonical_memory(),
                                       self.settings.memory_chars_per_section, Path(self.repo).name),
            "recent_outcomes": [{"proposal_id": o.proposal_id, "status": o.status,
                                 "summary": o.summary, "lessons": o.lessons} for o in outcomes],
            "act": {"open_proposals": [{"id": p.id, "title": p.title} for p in open_proposals]},
            "git": {"base_commit_sha": base_commit_sha, "dirty": bool(git_state.strip()),
                    "recent_commits": recent, "files": files},
        }

    def periodic_tick(self, base_commit_sha: str) -> list[Proposal]:
        open_proposals = self.act.list_proposals(ProposalStatus.proposed)
        if len(open_proposals) >= self.settings.max_open_proposals:
            log.info("tick.backlog_full", open=len(open_proposals))
            return []
        snap = self.perceive(base_commit_sha, open_proposals)
        self.last_snapshot_chars = len(json.dumps(snap, default=str))
        from navigator.runtime.drivers.base import DriverRequest
        from navigator.contracts.enums import Role
        plan = self.driver.run_role(DriverRequest(role=Role.planner, context={"repo": self.repo, "snapshot": snap}))
        created: list[Proposal] = []
        all_proposals = self.act.list_proposals()
        existing_hashes = {p.proposal_origin_hash for p in all_proposals}
        existing_titles = {_norm_title(p.title) for p in all_proposals}
        room = self.settings.max_open_proposals - len(open_proposals)
        for item in plan.proposals[: min(self.settings.max_proposals_per_tick, room)]:
            h = compute_origin_hash(self.repo, item["intended_changes"], item["affected_files"], "periodic", None)
            if h in existing_hashes or _norm_title(item["title"]) in existing_titles:
                continue
            existing_titles.add(_norm_title(item["title"]))
            p = Proposal(origin="periodic", proposal_origin_hash=h, title=item["title"],
                         rationale=item["rationale"], repo=self.repo, base_commit_sha=base_commit_sha,
                         intended_changes=item["intended_changes"], affected_files=item["affected_files"])
            # the backend may mint its own id — the stored proposal is the identity
            created.append(self.act.create_proposal(p))
        return created  # dry_run stops here: no worktree/git mutation

    def approval_poll(self) -> list:
        if self.settings.mode != "execute":
            return []
        runs = []
        with self.lock.acquire():
            for p in self.act.list_proposals(status=ProposalStatus.approved):
                # Correction 3: dedup guard — skip proposals that already have a run
                # (any status: a completed run must also block re-execution)
                if not self._may_run(p.id):
                    continue
                run = execute_proposal(p, act=self.act, orc=self.orc, driver=self.driver,
                                       runner=self.runner, settings=self.settings)
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

    def merge_poll(self) -> list:
        """Act on operator decisions at the merge gate; returns runs that finished."""
        if self.settings.mode != "execute":
            return []
        done = []
        with self.lock.acquire():
            for run in self.act.list_open_runs():
                if run.phase != RunPhase.awaiting_review:
                    continue
                decisions = {a.decision for a in
                             self.act.list_approvals(target=run.id, gate=ApprovalGate.merge)}
                if ApprovalDecision.declined in decisions:
                    done.append(decline_run(run, act=self.act, orc=self.orc, settings=self.settings))
                elif ApprovalDecision.approved in decisions:
                    run = merge_after_approval(run, act=self.act, orc=self.orc, driver=self.driver,
                                               runner=self.runner, settings=self.settings)
                    if run.status != RunStatus.executing:
                        done.append(run)
        return done

    def reconcile_on_startup(self) -> None:
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
            log.warning("reconcile.aborted_stale_run", run_id=run.id, preserved_worktree=run.worktree_path)


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
