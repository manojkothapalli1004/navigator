import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from navigator.runtime.loop import Navigator, QUOTA_DEFAULT_PAUSE_S
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.safety.errors import QuotaExhaustedError
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.enums import ProposalStatus, RiskTier, RunStatus

def _seed(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha

def _nav(tmp_path, repo, mode):
    s = Settings(mode=mode, worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"outcomes.jsonl"))
    return Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                     runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                     settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")

def test_dry_run_proposes_without_git_mutation(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="dry_run")
    nav.periodic_tick(base_commit_sha=sha)
    assert nav.act.list_proposals(status=ProposalStatus.proposed)
    branches = subprocess.run(["git","branch","--list","nav/*"], cwd=repo, capture_output=True, text=True).stdout
    assert branches.strip() == ""  # no worktree branches created

def test_perceive_returns_three_distinct_sources(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="dry_run")
    snap = nav.perceive(base_commit_sha=sha)
    # three non-conflated external sources (spec §8) + Navigator's own outcome trail
    assert set(snap.keys()) == {"orc_memory", "act", "git", "recent_outcomes",
                                 "operator_decisions"}

def test_approval_poll_executes_approved_proposal(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    nav.periodic_tick(base_commit_sha=sha)
    p = nav.act.list_proposals(status=ProposalStatus.proposed)[0]
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    runs = nav.approval_poll()
    assert len(runs) == 1
    assert nav.act.runs[runs[0].id].status in (RunStatus.executing,)  # awaiting merge

def test_reconcile_aborts_and_preserves_stale_runs(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    from navigator.contracts.run import Run
    stale = Run(proposal_id="p", base_commit_sha=sha, branch="nav/run-x", worktree_path=str(tmp_path/"wt/run-x"))
    nav.act.create_run(stale)
    nav.reconcile_on_startup()
    assert nav.act.runs[stale.id].status == RunStatus.aborted
    # a known_failures lesson is recorded for the aborted run (post-mortem trail)
    assert any(sec == "known_failures" and stale.id in note for sec, note, _ in nav.orc.updates)

def test_approval_poll_dedup_works_through_protocol_and_counts_completed_runs(tmp_path):
    # The dedup guard must use the ACTClient protocol (no Fake-only .runs attr)
    # and must skip proposals whose run already COMPLETED, not just open ones.
    from tests.support import ProtocolOnlyACT
    from navigator.contracts.proposal import Proposal, compute_origin_hash
    from navigator.contracts.run import Run
    repo, sha = _seed(tmp_path)
    inner = FakeACTClient()
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"outcomes.jsonl"))
    nav = Navigator(act=ProtocolOnlyACT(inner), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    p = Proposal(title="t", rationale="r", repo=str(repo), base_commit_sha=sha,
                 intended_changes="x", affected_files=["a.py"], origin="periodic",
                 proposal_origin_hash=compute_origin_hash(str(repo),"x",["a.py"],"periodic",None))
    inner.create_proposal(p)
    inner.set_proposal_status(p.id, ProposalStatus.approved)
    done = Run(proposal_id=p.id, base_commit_sha=sha, branch="nav/run-done",
               status=RunStatus.completed)
    inner.create_run(done)
    runs = nav.approval_poll()
    assert runs == []  # proposal already has a run (completed) — never re-execute

def test_periodic_tick_returns_stored_proposal_identity(tmp_path):
    # An HTTP ACT backend mints its own ids on create; periodic_tick must return
    # the stored proposals (server identity), not the pre-create drafts.
    from tests.support import ProtocolOnlyACT
    repo, sha = _seed(tmp_path)
    inner = FakeACTClient()

    class RemintingACT(ProtocolOnlyACT):
        def create_proposal(self, p):
            stored = p.model_copy(deep=True)
            stored.id = f"proposal-SERVER-{len(self._inner.proposals)}"
            return self._inner.create_proposal(stored)

    s = Settings(mode="dry_run", worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"outcomes.jsonl"))
    nav = Navigator(act=RemintingACT(inner), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    created = nav.periodic_tick(base_commit_sha=sha)
    assert created  # driver proposed at least one
    stored_ids = {q.id for q in inner.list_proposals()}
    assert {c.id for c in created} <= stored_ids


class _ScriptedPlanner:
    def __init__(self, titles):
        self.titles = titles
        self.calls = 0
        self.last_context = None
    def run_role(self, request):
        from navigator.runtime.drivers.base import DriverResult
        from navigator.contracts.enums import Role
        self.calls += 1
        self.last_context = request.context
        return DriverResult(role=Role.planner, proposals=[{
            "title": t, "rationale": "r", "intended_changes": f"change {i} {self.calls}",
            "affected_files": ["a.py"]} for i, t in enumerate(self.titles)])

def _scripted_nav(tmp_path, repo, planner, **settings_kw):
    s = Settings(mode="dry_run", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"), **settings_kw)
    return Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=planner,
                     runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                     settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")

def test_reworded_duplicate_title_not_reproposed(tmp_path):
    # observed live 2026-07→10: 100 open proposals, "Add timestamps to Navigator
    # log lines" x10 — LLM rewords intended_changes so the exact hash never matches
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner(["Add timestamps to Navigator log lines"])
    nav = _scripted_nav(tmp_path, repo, planner)
    assert len(nav.periodic_tick(base_commit_sha=sha)) == 1
    planner.titles = ["  add TIMESTAMPS to navigator log lines. "]
    assert nav.periodic_tick(base_commit_sha=sha) == []

def test_planner_skipped_when_open_backlog_full(tmp_path):
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner(["a", "b", "c"])
    nav = _scripted_nav(tmp_path, repo, planner, max_open_proposals=3)
    assert len(nav.periodic_tick(base_commit_sha=sha)) == 3
    assert nav.periodic_tick(base_commit_sha=sha) == []
    assert planner.calls == 1  # no LLM spend while nobody is reviewing

def test_planner_sees_open_titles_and_recent_commits(tmp_path):
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner(["first idea"])
    nav = _scripted_nav(tmp_path, repo, planner)
    nav.periodic_tick(base_commit_sha=sha)
    nav.periodic_tick(base_commit_sha=sha)
    snap = planner.last_context["snapshot"]
    assert [p["title"] for p in snap["act"]["open_proposals"]] == ["first idea"]
    assert "base" in snap["git"]["recent_commits"][0]

def test_planner_sees_tracked_file_list(tmp_path):
    repo, sha = _seed(tmp_path)
    (repo/"real.py").write_text("")
    subprocess.run(["git","add","real.py"], cwd=repo, check=True)
    subprocess.run(["git","commit","-m","f"], cwd=repo, check=True, capture_output=True)
    planner = _ScriptedPlanner(["x"])
    nav = _scripted_nav(tmp_path, repo, planner)
    nav.periodic_tick(base_commit_sha=sha)
    assert "real.py" in planner.last_context["snapshot"]["git"]["files"]

def test_reconcile_keeps_runs_parked_at_merge_gate(tmp_path):
    # a run awaiting the operator's merge decision is idle by design, not crashed;
    # aborting it on every daemon restart would throw away verified work
    from navigator.contracts.run import Run
    from navigator.contracts.enums import RunPhase
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    parked = Run(proposal_id="p", base_commit_sha=sha, branch="nav/run-p",
                 phase=RunPhase.awaiting_review, worktree_path=str(tmp_path/"wt/run-p"))
    nav.act.create_run(parked)
    nav.reconcile_on_startup()
    assert nav.act.runs[parked.id].status == RunStatus.executing
    assert nav.act.runs[parked.id].phase == RunPhase.awaiting_review


def _entry(day, text):
    return f"[2026-07-{day:02d}T00:00:00Z · s]\n{text}"

def test_planner_memory_is_trimmed_newest_and_repo_relevant_first(tmp_path):
    # live 2026-10-09: ~79k chars of ORC memory (mostly benched trader history)
    # went into every planner prompt; one cycle hit the 300s SDK timeout
    repo, sha = _seed(tmp_path)
    old_relevant = _entry(1, f"{repo.name} decision that still matters")
    filler = [_entry(d, "trader experiment " + "x" * 900) for d in range(2, 20)]
    newest = _entry(28, "newest status line")
    memory = {"sections": {"status": "\n\n".join([old_relevant, *filler, newest]),
                           "vision": "short vision"}}
    planner = _ScriptedPlanner(["x"])
    nav = _scripted_nav(tmp_path, repo, planner, memory_chars_per_section=3000)
    nav.orc = FakeORCClient(memory=memory)
    nav.periodic_tick(base_commit_sha=sha)
    mem = planner.last_context["snapshot"]["orc_memory"]
    assert len(mem["status"]) <= 3000
    assert "newest status line" in mem["status"]
    assert "decision that still matters" in mem["status"]
    assert mem["vision"] == "short vision"


def test_planner_sees_recent_outcomes(tmp_path):
    from navigator.runtime.outcome_store import OutcomeStore
    from navigator.contracts.outcome import Outcome
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner(["x"])
    nav = _scripted_nav(tmp_path, repo, planner)
    OutcomeStore(nav.settings.outcome_path).append(
        Outcome(run_id="run-1", proposal_id="p-1", status="failed", summary="verification failed",
                lessons=["git flag not on safe list"]))
    nav.periodic_tick(base_commit_sha=sha)
    outs = planner.last_context["snapshot"]["recent_outcomes"]
    assert outs == [{"proposal_id": "p-1", "status": "failed", "summary": "verification failed",
                     "lessons": ["git flag not on safe list"]}]


def test_stale_proposals_expire_so_backlog_cap_cannot_idle_forever(tmp_path):
    from datetime import datetime, timedelta, timezone
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner(["fresh idea"])
    nav = _scripted_nav(tmp_path, repo, planner, max_open_proposals=1, proposal_ttl_days=7)
    from navigator.contracts.proposal import Proposal
    old = Proposal(title="ancient", rationale="r", repo=str(repo), base_commit_sha=sha,
                   intended_changes="c", affected_files=["a.py"], origin="periodic",
                   proposal_origin_hash="h-old",
                   created_at=datetime.now(timezone.utc) - timedelta(days=8))
    nav.act.create_proposal(old)
    created = nav.periodic_tick(base_commit_sha=sha)
    assert nav.act.get_proposal(old.id).status == ProposalStatus.expired
    assert [p.title for p in created] == ["fresh idea"]


# --- near-duplicate suppression (live 2026-10-12: 3-4 reworded variants per idea) ---

def test_near_duplicate_detects_reworded_titles():
    from navigator.runtime.loop import _near_duplicate
    assert _near_duplicate(
        "Add edge-case regression tests for scheduler service and schedule storage",
        ["Add edge-case regression tests for the scheduler service and schedule storage"])
    assert _near_duplicate(
        "Add a CI-style guard test that every ping route enforces allowlist and kill-switch ordering",
        ["Add a CI-style guard test that every ping route enforces the allowlist and kill switch ordering"])

def test_near_duplicate_rejects_distinct_titles():
    from navigator.runtime.loop import _near_duplicate
    assert not _near_duplicate(
        "Add device-token registration endpoint with per-user ownership tests",
        ["Add ping history endpoint with allowlist-state snapshot tests"])
    assert not _near_duplicate("anything", [])
    assert not _near_duplicate("a", ["a"])  # stopword-only titles carry no content

def test_periodic_tick_creates_one_of_two_near_duplicates_in_a_batch(tmp_path):
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner([
        "Add edge-case regression tests for scheduler service and schedule storage",
        "Add edge-case regression tests for the scheduler service and schedule storage",
    ])
    nav = _scripted_nav(tmp_path, repo, planner)
    created = nav.periodic_tick(base_commit_sha=sha)
    assert len(created) == 1
    assert len(nav.act.list_proposals()) == 1

def test_periodic_tick_skips_near_duplicate_of_existing_any_status(tmp_path):
    repo, sha = _seed(tmp_path)
    planner = _ScriptedPlanner([
        "Add a CI-style guard test that every ping route enforces allowlist and kill-switch ordering"])
    nav = _scripted_nav(tmp_path, repo, planner)
    first = nav.periodic_tick(base_commit_sha=sha)
    assert len(first) == 1
    nav.act.set_proposal_status(first[0].id, ProposalStatus.declined)
    planner.titles = [
        "Add a CI-style guard test that every ping route enforces the allowlist and kill switch ordering",
        "Add ping history endpoint with allowlist-state snapshot tests",
    ]
    created = nav.periodic_tick(base_commit_sha=sha)
    assert [p.title for p in created] == ["Add ping history endpoint with allowlist-state snapshot tests"]


# --- N10: reconcile must not abort runs another live process is still building ---

def _stale_building_run(nav, tmp_path, sha):
    from navigator.contracts.run import Run
    from navigator.contracts.enums import RunPhase
    run = Run(proposal_id="p", base_commit_sha=sha, branch="nav/run-b", phase=RunPhase.building,
              worktree_path=str(tmp_path/"wt/run-b"))
    nav.act.create_run(run)
    return run

def test_reconcile_skips_when_lock_held_by_live_process(tmp_path):
    # live 2026-10-17 17:53: a daemon restart aborted run-01M55XC17WF7N8ZA2S5QNERA2D while
    # its once.sh builder process was alive and holding the lock
    from structlog.testing import capture_logs
    from navigator.runtime.lock import SingleFlightLock
    from navigator.contracts.enums import RunPhase
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    building = _stale_building_run(nav, tmp_path, sha)
    with SingleFlightLock(tmp_path/"nav.lock").acquire():  # this (live) process is the holder
        with capture_logs() as logs:
            nav.reconcile_on_startup()
        assert (tmp_path/"nav.lock").exists()  # the live holder's lock is untouched
    stored = nav.act.runs[building.id]
    assert stored.status == RunStatus.executing and stored.phase == RunPhase.building
    assert nav.orc.updates == []
    assert any(e["event"] == "reconcile.skipped_lock_held" for e in logs)
    assert not any(e["event"] == "reconcile.aborted_stale_run" for e in logs)

def test_reconcile_without_lock_still_aborts_building_run(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    building = _stale_building_run(nav, tmp_path, sha)
    assert not (tmp_path/"nav.lock").exists()
    nav.reconcile_on_startup()
    assert nav.act.runs[building.id].status == RunStatus.aborted
    assert any(sec == "known_failures" and building.id in note for sec, note, _ in nav.orc.updates)
    assert not (tmp_path/"nav.lock").exists()  # reconcile released the lock it took

def test_reconcile_recovers_lock_left_by_dead_process(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    building = _stale_building_run(nav, tmp_path, sha)
    (tmp_path/"nav.lock").write_text("999999999")  # PID far above pid_max: dead holder
    nav.reconcile_on_startup()
    assert nav.act.runs[building.id].status == RunStatus.aborted

def test_reconcile_propagates_unrelated_lock_errors(tmp_path):
    import pytest
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    def boom():
        raise RuntimeError("disk on fire")
    nav.lock.acquire = boom
    with pytest.raises(RuntimeError, match="disk on fire"):
        nav.reconcile_on_startup()


def test_planner_sees_operator_decisions_for_its_repo_only(tmp_path):
    from navigator.contracts.proposal import Proposal
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="dry_run")
    def mk(title, repo_path, status):
        p = nav.act.create_proposal(Proposal(origin="periodic", proposal_origin_hash=title,
                                             title=title, rationale="r", repo=repo_path,
                                             base_commit_sha=sha, intended_changes="x"))
        nav.act.set_proposal_status(p.id, status)
    mk("liked", str(repo), ProposalStatus.approved)
    mk("disliked", str(repo), ProposalStatus.declined)
    mk("other repo", "/elsewhere", ProposalStatus.approved)
    mk("still open", str(repo), ProposalStatus.proposed)
    decisions = nav.perceive(base_commit_sha=sha)["operator_decisions"]
    assert {"title": "liked", "decision": "approved"} in decisions
    assert {"title": "disliked", "decision": "declined"} in decisions
    assert all(d["title"] not in ("other repo", "still open") for d in decisions)


# --- N16: a quota-exhausted build pauses execution until the limit lifts -------------

class _QuotaDriver:
    def __init__(self, resets_at, text="You've hit your session limit"):
        self.resets_at = resets_at
        self.text = text
        self.calls = 0

    def run_role(self, request):
        self.calls += 1
        raise QuotaExhaustedError(self.text, marker="session limit", resets_at=self.resets_at)


def _approved_proposal(nav, repo, sha):
    return nav.act.create_proposal(Proposal(
        title="t", rationale="r", repo=str(repo), base_commit_sha=sha, intended_changes="x",
        affected_files=["navigator_touch.py"], risk_tier=RiskTier.low, origin="periodic",
        status="approved",
        proposal_origin_hash=compute_origin_hash(str(repo), "x", ["navigator_touch.py"],
                                                 "periodic", None)))


def test_quota_deferral_pauses_execution_until_resets_at(tmp_path):
    repo, sha = _seed(tmp_path)
    nav = _nav(tmp_path, repo, mode="execute")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    nav.driver = _QuotaDriver(resets_at=future)
    p = _approved_proposal(nav, repo, sha)
    assert nav.approval_poll() == []  # the deferred run did not execute
    assert nav.quota_deferral == {"error": "QuotaExhaustedError", "resets_at": future}
    assert nav.quota_paused_until == datetime.fromisoformat(future)
    runs = nav.act.list_runs_for_proposal(p.id)
    assert len(runs) == 1 and runs[0].status == RunStatus.aborted
    assert nav.driver.calls == 1
    # paused: no execution, no new run, nothing deferred by this poll
    assert nav.approval_poll() == []
    assert nav.driver.calls == 1
    assert nav.quota_deferral is None
    assert nav.quota_paused_until == datetime.fromisoformat(future)
    # the pause lapsed: the grant bought exactly one rebuild, which deferred and re-paused
    nav.quota_paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert nav.approval_poll() == []
    assert nav.driver.calls == 2
    assert len(nav.act.list_runs_for_proposal(p.id)) == 2
    assert nav.quota_paused_until == datetime.fromisoformat(future)
    # no reset time known: pause for about QUOTA_DEFAULT_PAUSE_S
    (tmp_path / "two").mkdir()
    repo2, sha2 = _seed(tmp_path / "two")
    nav2 = _nav(tmp_path / "two", repo2, mode="execute")
    nav2.driver = _QuotaDriver(resets_at=None)
    _approved_proposal(nav2, repo2, sha2)
    assert nav2.approval_poll() == []
    assert nav2.quota_deferral == {"error": "QuotaExhaustedError", "resets_at": None}
    remaining = (nav2.quota_paused_until - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < remaining <= QUOTA_DEFAULT_PAUSE_S
