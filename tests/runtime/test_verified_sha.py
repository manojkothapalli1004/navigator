import subprocess
from pathlib import Path
from navigator.runtime.executor import execute_proposal, merge_after_approval
from navigator.runtime.outcome_store import OutcomeStore
from navigator.observability.notify import NotifyingACT
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


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                   check=True, capture_output=True)


class _Recorder:
    def __init__(self):
        self.sent = []
    def send(self, text):
        self.sent.append(text)


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
    _git(run.worktree_path, "commit", "--allow-empty", "-m", "sneaky")
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


def test_squash_tree_equals_verified_tree(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    verified = run.verified_sha
    assert verified
    _approve(d, run)
    merge_after_approval(run, squash=True, **d)
    assert d["act"].runs[run.id].status == RunStatus.completed
    assert _head(repo, "HEAD^{tree}") == _head(repo, f"{verified}^{{tree}}")
    parents = subprocess.run(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=repo,
                             capture_output=True, text=True).stdout.split()
    assert len(parents) == 2  # a single-parent squash commit, not a merge
    assert _head(repo) != verified


def test_squash_blocked_when_branch_moved_after_verification(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    verified = run.verified_sha
    Path(run.worktree_path, "extra.txt").write_text("unverified\n")
    _git(run.worktree_path, "add", "extra.txt")
    _git(run.worktree_path, "commit", "-m", "post-verification")
    new = _head(run.worktree_path)
    assert new != verified
    _approve(d, run)
    before = _head(repo)
    merge_after_approval(run, squash=True, **d)
    stored = d["act"].runs[run.id]
    assert stored.status == RunStatus.executing
    assert stored.phase == RunPhase.awaiting_review
    assert _head(repo) == before
    tracked = _tracked(repo)
    assert "extra.txt" not in tracked and "navigator_touch.py" not in tracked
    blocked = [e for e in d["act"].events if e.kind == "run.merge_blocked"]
    assert blocked[-1].payload["reason"] == \
        f"branch changed since verification: {new[:12]} != {verified[:12]}"


def test_deleted_run_branch_fails_the_run(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    verified = run.verified_sha
    assert verified
    _git(repo, "worktree", "remove", "--force", run.worktree_path)
    _git(repo, "branch", "-D", run.branch)
    assert _head(repo, f"refs/heads/{run.branch}") in ("", f"refs/heads/{run.branch}")
    _approve(d, run)
    before = _head(repo)
    merge_after_approval(run, **d)
    stored = d["act"].runs[run.id]
    assert stored.status == RunStatus.failed
    assert stored.phase == RunPhase.finalizing
    assert stored.ended_at is not None
    assert _head(repo) == before
    reason = f"run branch missing; verified {verified[:12]}"
    failed = [e for e in d["act"].events if e.kind == "run.failed"]
    assert failed and failed[-1].payload["error"] == reason
    assert not [e for e in d["act"].events if e.kind == "run.merge_blocked"]
    notes = [n for sec, n, _ in d["orc"].updates if sec == "known_failures" and run.id in n]
    assert notes and reason in notes[-1]
    outcome_path = getattr(d["settings"], "outcome_path", None)
    if outcome_path:
        outs = [o for o in OutcomeStore(outcome_path).latest(50) if o.run_id == run.id]
        assert outs and outs[0].status == "failed" and outs[0].summary == reason


def test_merge_blocked_notifies_once_per_reason(tmp_path):
    repo, sha = _seed(tmp_path); d = _deps(tmp_path, repo)
    rec = _Recorder()
    d = dict(d, act=NotifyingACT(d["act"], rec))
    # a tracked file in the main checkout so it can be dirtied
    Path(repo, "tracked.txt").write_text("v1\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "tracked")
    sha = _head(repo)
    run = execute_proposal(_proposal(repo, sha), **d)
    _approve(d, run)
    Path(repo, "tracked.txt").write_text("operator edit\n")
    merge_after_approval(run, **d)
    merge_after_approval(run, **d)
    waiting = [t for t in rec.sent if "is waiting" in t]
    assert len(waiting) == 1 and "uncommitted changes" in waiting[0]
    _git(repo, "checkout", "--", "tracked.txt")
    _git(run.worktree_path, "commit", "--allow-empty", "-m", "sneaky")
    merge_after_approval(run, **d)
    merge_after_approval(run, **d)
    waiting = [t for t in rec.sent if "is waiting" in t]
    assert len(waiting) == 2
    assert "branch changed since verification" in waiting[1]
    assert d["act"].runs[run.id].status == RunStatus.executing
    assert len([e for e in d["act"].events if e.kind == "run.merge_blocked"]) == 4
