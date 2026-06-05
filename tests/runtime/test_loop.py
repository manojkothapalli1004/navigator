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
    s = Settings(mode=mode, worktree_root=str(tmp_path/"wt"))
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
    assert set(snap.keys()) == {"orc_memory", "act", "git"}

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
