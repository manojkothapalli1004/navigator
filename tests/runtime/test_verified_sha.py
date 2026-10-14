import subprocess
from navigator.runtime.executor import execute_proposal, merge_after_approval
from navigator.contracts.enums import RunPhase, ApprovalGate, ApprovalDecision, RunStatus
from tests.runtime.test_executor import _seed, _proposal, _deps


def _head(cwd, rev="HEAD"):
    return subprocess.run(["git", "rev-parse", rev], cwd=cwd, capture_output=True,
                          text=True).stdout.strip()


def _approve(d, run):
    a = d["act"].list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    d["act"].decide_approval(a.id, ApprovalDecision.approved, actor="manoj")


def _tracked(repo):
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout


def test_verified_sha_recorded_on_success(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    assert run.phase == RunPhase.awaiting_review
    wt_head = _head(run.worktree_path)
    assert run.verified_sha and run.verified_sha == wt_head
    assert d["act"].runs[run.id].verified_sha == wt_head
    ev = [e for e in d["act"].events if e.kind == "run.awaiting_review"][0]
    assert ev.payload["verified_sha"] == wt_head


def test_commit_after_verification_blocks_merge(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    verified = run.verified_sha
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "--allow-empty", "-m", "sneaky"], cwd=run.worktree_path,
                   check=True, capture_output=True)
    new = _head(run.worktree_path)
    assert new != verified
    _approve(d, run)
    before = _head(repo)
    merge_after_approval(run, **d)
    stored = d["act"].runs[run.id]
    assert stored.status == RunStatus.executing
    assert stored.phase == RunPhase.awaiting_review
    blocked = [e for e in d["act"].events if e.kind == "run.merge_blocked"]
    assert blocked
    reason = blocked[-1].payload["reason"]
    assert reason == f"branch changed since verification: {new[:12]} != {verified[:12]}"
    assert _head(repo) == before
    assert "navigator_touch.py" not in _tracked(repo)


def test_unchanged_branch_merges_and_message_names_verified_sha(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _approve(d, run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)
    msg = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    assert msg.endswith(f" verified {run.verified_sha[:12]}")
    # the verified commit itself is what landed
    assert _head(repo, "HEAD^2") == run.verified_sha


def test_old_run_without_verified_sha_merges_as_before(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    run.verified_sha = None
    d["act"].update_run(run)
    _approve(d, run)
    merge_after_approval(run, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)
    assert not [e for e in d["act"].events if e.kind == "run.merge_blocked"]


def test_squash_merge_with_verified_sha(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _approve(d, run)
    merge_after_approval(run, squash=True, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert "navigator_touch.py" in _tracked(repo)
