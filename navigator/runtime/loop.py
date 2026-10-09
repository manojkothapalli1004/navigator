import subprocess
from datetime import datetime, timezone
from pathlib import Path
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import ProposalStatus, RunStatus, RunPhase
from navigator.runtime.executor import execute_proposal
from navigator.runtime.lock import SingleFlightLock
from navigator.observability.logging import get_logger

log = get_logger("navigator.loop")

class Navigator:
    def __init__(self, *, act, orc, driver, runner, settings, repo: str, lock_path):
        self.act = act; self.orc = orc; self.driver = driver; self.runner = runner
        self.settings = settings; self.repo = repo
        self.lock = SingleFlightLock(lock_path)

    def perceive(self, base_commit_sha: str, open_proposals: list[Proposal] | None = None) -> dict:
        if open_proposals is None:
            open_proposals = self.act.list_proposals(ProposalStatus.proposed)
        git_state = subprocess.run(["git","status","--porcelain"], cwd=self.repo,
                                   capture_output=True, text=True).stdout
        # recent commits let the planner see what already shipped; without them it
        # kept proposing finished work (outcome store, timestamps) for weeks
        recent = subprocess.run(["git","log","--oneline","-15"], cwd=self.repo,
                                capture_output=True, text=True).stdout.splitlines()
        return {
            "orc_memory": self.orc.get_canonical_memory(),
            "act": {"open_proposals": [{"id": p.id, "title": p.title} for p in open_proposals]},
            "git": {"base_commit_sha": base_commit_sha, "dirty": bool(git_state.strip()),
                    "recent_commits": recent},
        }

    def periodic_tick(self, base_commit_sha: str) -> list[Proposal]:
        open_proposals = self.act.list_proposals(ProposalStatus.proposed)
        if len(open_proposals) >= self.settings.max_open_proposals:
            log.info("tick.backlog_full", open=len(open_proposals))
            return []
        snap = self.perceive(base_commit_sha, open_proposals)
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
                if self.act.list_runs_for_proposal(p.id):
                    continue
                run = execute_proposal(p, act=self.act, orc=self.orc, driver=self.driver,
                                       runner=self.runner, settings=self.settings)
                # Correction 3: proposal stays approved; the dedup guard above prevents re-execution
                runs.append(run)
        return runs

    def reconcile_on_startup(self) -> None:
        for run in self.act.list_open_runs():
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


def _norm_title(title: str) -> str:
    return " ".join(title.lower().strip(" .").split())
