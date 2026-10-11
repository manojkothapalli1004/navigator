import json
import subprocess
from pathlib import Path
from navigator.runtime.loop import Navigator
from navigator.runtime.fleet import Fleet
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings, RepoConfig
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision, RunStatus


def _repo(tmp_path, name, with_tests_in=None):
    repo = tmp_path/name; repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    if with_tests_in:
        (repo/with_tests_in/"tests").mkdir(parents=True)
        (repo/with_tests_in/"tests"/"keep.txt").write_text("")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


def _fleet(tmp_path, repos):
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))
    act, orc, driver = FakeACTClient(), FakeORCClient(), FakeSubagentDriver()
    navs = [Navigator(act=act, orc=orc, driver=driver,
                      runner=CommandRunner(allowlist=rc.allowed_commands or s.allowed_commands,
                                           worktree_root=tmp_path/"wt"),
                      settings=s, repo=rc.path, lock_path=tmp_path/"nav.lock", workdir=rc.workdir)
            for rc in repos]
    return Fleet(navs), act


def test_planner_rotates_one_repo_per_tick(tmp_path):
    a, b = _repo(tmp_path, "alpha"), _repo(tmp_path, "beta")
    fleet, act = _fleet(tmp_path, [RepoConfig(path=str(a)), RepoConfig(path=str(b))])
    planned = []
    for nav in fleet.navigators:
        real = nav.periodic_tick
        nav.periodic_tick = (lambda real=real, nav=nav: planned.append(nav.repo) or real())
    for _ in range(3):
        fleet.periodic_tick()
    assert planned == [str(a), str(b), str(a)]


def test_each_navigator_only_executes_and_merges_its_own_repo(tmp_path):
    a, b = _repo(tmp_path, "alpha"), _repo(tmp_path, "beta")
    fleet, act = _fleet(tmp_path, [RepoConfig(path=str(a)), RepoConfig(path=str(b))])
    fleet.periodic_tick(); fleet.periodic_tick()
    for p in act.list_proposals():
        act.set_proposal_status(p.id, ProposalStatus.approved)
    runs = fleet.approval_poll()
    assert len(runs) == 2
    for r in runs:
        appr = act.list_approvals(target=r.id, gate=ApprovalGate.merge)[0]
        act.decide_approval(appr.id, ApprovalDecision.approved, actor="m")
    assert len(fleet.merge_poll()) == 2
    for repo in (a, b):  # each change landed in its own repo, not the other one
        assert "navigator_touch.py" in subprocess.run(["git", "ls-files"], cwd=repo,
                                                      capture_output=True, text=True).stdout
    assert all(r.status == RunStatus.completed for r in act.runs.values())


def test_backlog_cap_is_per_repo(tmp_path):
    a, b = _repo(tmp_path, "alpha"), _repo(tmp_path, "beta")
    fleet, act = _fleet(tmp_path, [RepoConfig(path=str(a)), RepoConfig(path=str(b))])
    for nav in fleet.navigators:
        nav.settings.max_open_proposals = 1
    assert fleet.periodic_tick()  # alpha fills its backlog
    assert fleet.periodic_tick()  # beta still plans: alpha's backlog doesn't block it


def test_verifier_runs_in_repo_workdir(tmp_path):
    # waker keeps its python project (and tests) under backend/
    repo = _repo(tmp_path, "waker")
    (repo/"backend").mkdir(); (repo/"backend"/"app.py").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "backend"], cwd=repo, check=True, capture_output=True)
    seen = {}
    class _Runner(CommandRunner):
        def run(self, argv, cwd):
            seen["cwd"] = Path(cwd); return super().run(argv, cwd)
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"o.jsonl"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=_Runner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"l", workdir="backend")
    nav.periodic_tick()
    p = nav.act.list_proposals()[0]
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    run = nav.approval_poll()[0]
    assert seen["cwd"] == Path(run.worktree_path).resolve()/"backend"


class _RecordingPlanner(FakeSubagentDriver):
    def __init__(self):
        self.contexts = []
    def run_role(self, request):
        from navigator.contracts.enums import Role
        if request.role == Role.planner:
            self.contexts.append(request.context)
        return super().run_role(request)


def _research_nav(tmp_path, **kw):
    repo = _repo(tmp_path, "trader")
    (repo/"research_manager").mkdir()
    (repo/"research_manager"/"morning_brief.md").write_text("## Brief\norb@ETH 12 trades, net -0.40\n")
    s = Settings(mode="dry_run", worktree_root=str(tmp_path/"wt"), outcome_path=str(tmp_path/"o.jsonl"))
    planner = _RecordingPlanner()
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=planner,
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"l",
                    brief="Propose at most ONE paper-only experiment with a kill rule.",
                    context_files=["research_manager/morning_brief.md", "research_manager/missing.json"],
                    **kw)
    return nav, planner


def test_repo_brief_and_context_files_reach_the_planner(tmp_path):
    nav, planner = _research_nav(tmp_path)
    nav.periodic_tick()
    snap = planner.contexts[0]["snapshot"]
    assert snap["repo_brief"] == "Propose at most ONE paper-only experiment with a kill rule."
    assert "orb@ETH 12 trades" in snap["context_files"]["research_manager/morning_brief.md"]
    assert snap["context_files"]["research_manager/missing.json"] is None


def test_repo_plans_at_most_once_per_interval(tmp_path):
    # trader research loop: one experiment proposal per day, not one per tick
    nav, planner = _research_nav(tmp_path, min_plan_interval_hours=24, max_proposals_per_tick=1)
    assert len(nav.periodic_tick()) == 1
    assert nav.periodic_tick() == []
    assert len(planner.contexts) == 1  # no model call while inside the interval


def test_repo_scoped_max_proposals_per_tick(tmp_path):
    nav, _ = _research_nav(tmp_path, max_proposals_per_tick=1)
    nav.driver = type("P", (), {"run_role": lambda self, r: __import__(
        "navigator.runtime.drivers.base", fromlist=["DriverResult"]).DriverResult(
        role=r.role, proposals=[{"title": f"t{i}", "rationale": "r", "intended_changes": f"c{i}",
                                 "affected_files": ["a.py"]} for i in range(3)])})()
    assert len(nav.periodic_tick()) == 1
