import subprocess
from pathlib import Path
from navigator.runtime.loop import Navigator
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.enums import ProposalStatus, RunStatus

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
    assert set(snap.keys()) == {"orc_memory", "act", "git", "recent_outcomes"}

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
