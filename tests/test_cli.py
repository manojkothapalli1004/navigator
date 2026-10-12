import io
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from navigator.cli import run_cli
from navigator.runtime.loop import Navigator
from navigator.runtime.worktree import create_worktree
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.ids import new_id
from navigator.contracts.enums import (ProposalStatus, ApprovalGate, ApprovalDecision,
                                       RunStatus, RunPhase)


def _nav(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    s = Settings(mode="execute", worktree_root=str(tmp_path/"wt"),
                 outcome_path=str(tmp_path/"outcomes.jsonl"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=FakeSubagentDriver(),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path/"wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path/"nav.lock")
    nav.periodic_tick(base_commit_sha=sha)
    return nav, nav.act.list_proposals()[0]


def _cli(nav, *argv):
    out = io.StringIO()
    code = run_cli(list(argv), act=nav.act, out=out)
    return code, out.getvalue()


def test_status_lists_open_proposals_and_parked_runs(tmp_path):
    nav, p = _nav(tmp_path)
    code, out = _cli(nav, "status")
    assert code == 0 and p.id in out and p.title in out
    _cli(nav, "approve", p.id)
    run = nav.approval_poll()[0]
    code, out = _cli(nav, "status")
    assert run.id in out and "awaiting_review" in out


def test_approve_then_merge_through_cli(tmp_path):
    nav, p = _nav(tmp_path)
    assert _cli(nav, "approve", p.id)[0] == 0
    assert nav.act.get_proposal(p.id).status == ProposalStatus.approved
    run = nav.approval_poll()[0]
    code, out = _cli(nav, "diff", run.id)
    assert code == 0 and "navigator_touch.py" in out
    # the reviewer sees what verification actually ran before the diff
    assert "verification:" in out and "python3 -m py_compile" in out and "rc=0" in out
    assert "model calls:" in out and "$" not in out.splitlines()[0]  # no fake dollar figure
    assert _cli(nav, "merge", run.id)[0] == 0
    nav.merge_poll()
    assert nav.act.runs[run.id].status == RunStatus.completed


def test_decline_and_reject(tmp_path):
    nav, p = _nav(tmp_path)
    _cli(nav, "decline", p.id)
    assert nav.act.get_proposal(p.id).status == ProposalStatus.declined
    nav.act.set_proposal_status(p.id, ProposalStatus.approved)
    run = nav.approval_poll()[0]
    assert _cli(nav, "reject", run.id)[0] == 0
    nav.merge_poll()
    assert nav.act.runs[run.id].status == RunStatus.aborted


def test_unknown_ids_fail_with_nonzero_exit(tmp_path):
    nav, _ = _nav(tmp_path)
    assert _cli(nav, "merge", "run-nope")[0] != 0
    assert _cli(nav, "approve", "proposal-nope")[0] != 0
    assert _cli(nav, "bogus")[0] != 0


def test_retry_allows_exactly_one_more_run_after_failure(tmp_path):
    # a failed run blocks its proposal forever by design; retry is the operator's
    # explicit, single-use override
    nav, p = _nav(tmp_path)
    _cli(nav, "approve", p.id)
    run = nav.approval_poll()[0]
    run.status = RunStatus.failed; run.phase = RunPhase.finalizing
    nav.act.update_run(run)
    assert nav.approval_poll() == []          # blocked without retry
    assert _cli(nav, "retry", p.id)[0] == 0
    assert len(nav.approval_poll()) == 1      # one more run
    latest = [r for r in nav.act.runs.values() if r.id != run.id][0]
    latest.status = RunStatus.failed; nav.act.update_run(latest)
    assert nav.approval_poll() == []          # retry consumed


def test_retry_refused_while_a_run_is_open_or_completed(tmp_path):
    nav, p = _nav(tmp_path)
    _cli(nav, "approve", p.id)
    nav.approval_poll()
    assert _cli(nav, "retry", p.id)[0] != 0


# ---- cleanup -------------------------------------------------------------------

def _cleanup_world(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    act = FakeACTClient()
    p = act.create_proposal(Proposal(title="t", rationale="r", repo=str(repo),
                                     base_commit_sha=sha, intended_changes="x",
                                     affected_files=["a.py"], origin="periodic",
                                     proposal_origin_hash="h"))
    now = datetime.now(timezone.utc)
    runs = {}
    for name, status, ended in (("done", RunStatus.completed, now),
                                ("live", RunStatus.executing, None),
                                ("recent_fail", RunStatus.failed, now - timedelta(days=1)),
                                ("old_fail", RunStatus.failed, now - timedelta(days=10))):
        rid = new_id("run")
        h = create_worktree(repo=repo, base_commit_sha=sha, run_id=rid,
                            worktree_root=tmp_path/"wt")
        r = Run(id=rid, proposal_id=p.id, base_commit_sha=sha, branch=h.branch,
                worktree_path=str(h.path), status=status, ended_at=ended)
        act.create_run(r)
        runs[name] = r
    return repo, act, runs


def _cleanup_cli(act, tmp_path, *argv):
    out = io.StringIO()
    code = run_cli(["cleanup", *argv], act=act, out=out, worktree_root=str(tmp_path/"wt"))
    return code, out.getvalue()


def _branch_exists(repo, branch):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          cwd=repo, capture_output=True).returncode == 0


def test_cleanup_dry_run_lists_actions_and_changes_nothing(tmp_path):
    repo, act, runs = _cleanup_world(tmp_path)
    code, out = _cleanup_cli(act, tmp_path)
    assert code == 0
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines and all(l.startswith("would ") for l in lines)
    for name in ("done", "old_fail"):
        assert f"would remove worktree" in out and runs[name].id in out
        assert f"would delete branch nav/{runs[name].id}" in out
    assert "would prune worktrees" in out
    for name in ("live", "recent_fail"):
        assert runs[name].id not in out
    for r in runs.values():
        assert Path(r.worktree_path).exists()
        assert _branch_exists(repo, r.branch)


def test_cleanup_apply_removes_finished_runs_only(tmp_path):
    repo, act, runs = _cleanup_world(tmp_path)
    code, out = _cleanup_cli(act, tmp_path, "--apply")
    assert code == 0, out
    assert "would" not in out
    for name in ("done", "old_fail"):
        assert not Path(runs[name].worktree_path).exists()
        assert not _branch_exists(repo, runs[name].branch)
    # NEGATIVE: executing run and a failed run ended 1 day ago are untouched
    for name in ("live", "recent_fail"):
        assert Path(runs[name].worktree_path).exists()
        assert _branch_exists(repo, runs[name].branch)
    assert _branch_exists(repo, "main")
    listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                             capture_output=True, text=True).stdout
    assert runs["done"].id not in listing and runs["live"].id in listing
    # a second run has nothing left to do for the finished runs
    code, out = _cleanup_cli(act, tmp_path)
    assert code == 0 and runs["done"].id not in out


class _FailingProposalACT:
    """FakeACT wrapper: get_proposal raises for one proposal id, and the run that
    hits it is listed first so later runs prove the loop keeps going."""

    def __init__(self, inner, bad_proposal_id, bad_run_id):
        self.inner = inner
        self.bad_proposal_id = bad_proposal_id
        self.bad_run_id = bad_run_id

    def get_proposal(self, proposal_id):
        if proposal_id == self.bad_proposal_id:
            raise RuntimeError("ACT 503")
        return self.inner.get_proposal(proposal_id)

    def list_all_runs(self):
        runs = self.inner.list_all_runs()
        return sorted(runs, key=lambda r: r.id != self.bad_run_id)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_cleanup_continues_past_a_run_whose_lookup_errors(tmp_path):
    repo, act, runs = _cleanup_world(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    bad_p = act.create_proposal(Proposal(title="t2", rationale="r", repo=str(repo),
                                         base_commit_sha=sha, intended_changes="x",
                                         affected_files=["b.py"], origin="periodic",
                                         proposal_origin_hash="h2"))
    rid = new_id("run")
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id=rid, worktree_root=tmp_path/"wt")
    bad = act.create_run(Run(id=rid, proposal_id=bad_p.id, base_commit_sha=sha,
                             branch=h.branch, worktree_path=str(h.path),
                             status=RunStatus.completed, ended_at=datetime.now(timezone.utc)))
    failing = _FailingProposalACT(act, bad_p.id, bad.id)
    assert failing.list_all_runs()[0].id == bad.id  # errored run is processed first

    code, out = _cleanup_cli(failing, tmp_path, "--apply")
    assert code == 1, out
    assert f"error {bad.id}: RuntimeError" in out
    # the other eligible runs are still cleaned
    for name in ("done", "old_fail"):
        assert not Path(runs[name].worktree_path).exists()
        assert not _branch_exists(repo, runs[name].branch)
    # NEGATIVE: the errored run's worktree and branch are untouched
    assert Path(bad.worktree_path).exists()
    assert _branch_exists(repo, bad.branch)
    for name in ("live", "recent_fail"):
        assert Path(runs[name].worktree_path).exists()
        assert _branch_exists(repo, runs[name].branch)


def test_cleanup_ignores_worktrees_outside_root(tmp_path):
    repo, act, runs = _cleanup_world(tmp_path)
    out = io.StringIO()
    code = run_cli(["cleanup"], act=act, out=out, worktree_root=str(tmp_path/"elsewhere"))
    assert code == 0
    assert "remove worktree" not in out.getvalue()


def test_cleanup_reads_worktree_root_from_env(tmp_path, monkeypatch):
    repo, act, runs = _cleanup_world(tmp_path)
    monkeypatch.setenv("NAVIGATOR_WORKTREE_ROOT", str(tmp_path/"wt"))
    out = io.StringIO()
    assert run_cli(["cleanup"], act=act, out=out) == 0
    assert runs["done"].id in out.getvalue()
    assert Path(runs["done"].worktree_path).exists()


def test_cleanup_takes_no_id(tmp_path):
    act = FakeACTClient()
    out = io.StringIO()
    assert run_cli(["cleanup", "run-x"], act=act, out=out,
                   worktree_root=str(tmp_path/"wt")) == 2
    assert run_cli(["cleanup", "--apply", "extra"], act=act, out=io.StringIO(),
                   worktree_root=str(tmp_path/"wt")) == 2
