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

    def perceive(self, base_commit_sha: str) -> dict:
        git_state = subprocess.run(["git","status","--porcelain"], cwd=self.repo,
                                   capture_output=True, text=True).stdout
        return {
            "orc_memory": self.orc.get_canonical_memory(),
            "act": {"open_proposals": [p.id for p in self.act.list_proposals(ProposalStatus.proposed)]},
            "git": {"base_commit_sha": base_commit_sha, "dirty": bool(git_state.strip())},
        }

    def periodic_tick(self, base_commit_sha: str) -> list[Proposal]:
        snap = self.perceive(base_commit_sha)
        from navigator.runtime.drivers.base import DriverRequest
        from navigator.contracts.enums import Role
        plan = self.driver.run_role(DriverRequest(role=Role.planner, context={"repo": self.repo, "snapshot": snap}))
        created: list[Proposal] = []
        existing_hashes = {p.proposal_origin_hash for p in self.act.list_proposals()}
        for item in plan.proposals[: self.settings.max_proposals_per_tick]:
            h = compute_origin_hash(self.repo, item["intended_changes"], item["affected_files"], "periodic", None)
            if h in existing_hashes:
                continue
            p = Proposal(origin="periodic", proposal_origin_hash=h, title=item["title"],
                         rationale=item["rationale"], repo=self.repo, base_commit_sha=base_commit_sha,
                         intended_changes=item["intended_changes"], affected_files=item["affected_files"])
            self.act.create_proposal(p); created.append(p)
        return created  # dry_run stops here: no worktree/git mutation

    def approval_poll(self) -> list:
        if self.settings.mode != "execute":
            return []
        runs = []
        with self.lock.acquire():
            for p in self.act.list_proposals(status=ProposalStatus.approved):
                # Correction 3: dedup guard — skip proposals that already have a run
                if any(r.proposal_id == p.id for r in self.act.runs.values()):
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
