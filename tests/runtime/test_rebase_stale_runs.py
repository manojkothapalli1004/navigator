"""N12d: after a merge, parked sibling runs of the same repo are rebased onto the new
HEAD and re-verified with their recorded commands (no model call); an audit carries
over on an identical diff; a conflict or re-verify failure grants one rebuild."""
import io
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from navigator.cli import ACTOR, AUDITOR, _audited, is_audited
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.config import Settings
from navigator.contracts.enums import (ApprovalDecision, ApprovalGate, ProposalStatus, Role,
                                       RunPhase, RunStatus)
from navigator.contracts.event import Event
from navigator.contracts.proposal import Proposal
from navigator.runtime import executor as ex
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.loop import Navigator
from navigator.safety.command_runner import CommandRunner

PY = ["python3", "-m", "py_compile"]
DEFAULT_BUILDS = {"A": [("a.py", "A = 1\n")], "B": [("b.py", "B = 1\n")]}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def _head(cwd):
    return _git(cwd, "rev-parse", "HEAD").strip()


def _clean(cwd):
    return _git(cwd, "status", "--porcelain", "--untracked-files=no").strip() == ""


class _Driver:
    """Scripted builder/verifier keyed by proposal title; the planner is never called."""
    def __init__(self, builds, verifies):
        self.builds, self.verifies, self.calls = builds, verifies, 0

    def run_role(self, req):
        self.calls += 1
        title = req.context["proposal"]["title"]
        if req.role == Role.builder:
            return DriverResult(role=Role.builder, file_writes=list(self.builds[title]))
        cmds = self.verifies.get(title) or [PY + [f] for f in req.context["changed_files"]]
        return DriverResult(role=Role.verifier, commands=cmds)


def _setup(tmp_path, builds=DEFAULT_BUILDS, verifies=None, squash=False):
    """Two approved proposals A and B, built from one base in a single approval_poll."""
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "lib.py").write_text("KEEP = 1\n")
    _git(repo, "add", "lib.py"); _git(repo, "commit", "-m", "base")
    s = Settings(mode="execute", worktree_root=str(tmp_path / "wt"),
                 outcome_path=str(tmp_path / "outcomes.jsonl"))
    nav = Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=_Driver(builds, verifies or {}),
                    runner=CommandRunner(allowlist=s.allowed_commands, worktree_root=tmp_path / "wt"),
                    settings=s, repo=str(repo), lock_path=tmp_path / "nav.lock", squash=squash)
    titles = {}
    for title, writes in builds.items():
        p = nav.act.create_proposal(Proposal(
            origin="periodic", proposal_origin_hash=f"hash-{title}", title=title, rationale="r",
            repo=str(repo), base_commit_sha=_head(repo), intended_changes=title,
            affected_files=[f for f, _ in writes]))
        nav.act.set_proposal_status(p.id, ProposalStatus.approved)
        titles[p.id] = title
    runs = {titles[r.proposal_id]: r for r in nav.approval_poll()}
    assert all(r.phase == RunPhase.awaiting_review for r in runs.values())
    return nav, repo, runs["A"], runs["B"]


def _merge(nav, run):
    appr = nav.act.list_approvals(target=run.id, gate=ApprovalGate.merge)[0]
    nav.act.decide_approval(appr.id, ApprovalDecision.approved, actor=ACTOR)
    return nav.merge_poll()


def _events(nav, run, kind=None):
    return [e for e in nav.act.events if e.payload.get("run_id") == run.id
            and (kind is None or e.kind == kind)]


def _grants(nav, run):
    return nav.act.list_approvals(target=run.proposal_id, gate=ApprovalGate.review,
                                  decision=ApprovalDecision.approved)


def _audits(nav, run):
    return nav.act.list_approvals(target=run.id, gate=ApprovalGate.review,
                                  decision=ApprovalDecision.approved)


def test_sibling_is_rebased_and_reverified_after_merge(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    old_base, old_sha = b.base_commit_sha, b.verified_sha
    assert [r.id for r in _merge(nav, a)] == [a.id]
    head = _head(repo)
    sb = nav.act.runs[b.id]
    assert sb.phase == RunPhase.awaiting_review and sb.status == RunStatus.executing
    assert sb.base_commit_sha == head and head != old_base
    assert sb.verified_sha == _head(sb.worktree_path) and sb.verified_sha != old_sha
    assert [e["argv_list"] for e in sb.verification] == [PY + ["b.py"]]
    assert all(e["returncode"] == 0 for e in sb.verification)
    ev = _events(nav, b, "run.awaiting_review")[-1].payload
    assert ev["rebased_from"] == old_sha and ev["merged_run_id"] == a.id
    assert ev["verified_sha"] == sb.verified_sha and "b.py" in ev["diff"]
    assert _clean(sb.worktree_path) and _clean(repo)
    assert [r.id for r in _merge(nav, sb)] == [b.id]  # a later merge of B succeeds
    assert nav.act.runs[b.id].status == RunStatus.completed
    assert {"a.py", "b.py"} <= set(_git(repo, "ls-files").split())


def test_audit_is_carried_forward_on_identical_diff(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    assert _audited(nav.act, [b.id, "--note", "x"], io.StringIO()) == 0
    assert is_audited(nav.act, b)
    _merge(nav, a)
    sb = nav.act.runs[b.id]
    assert sb.verified_sha != b.verified_sha
    assert is_audited(nav.act, sb) is True
    carried = [e for e in _events(nav, b, "run.audited")
               if e.payload.get("verified_sha") == sb.verified_sha]
    assert len(carried) == 1 and carried[0].payload["actor"] == AUDITOR
    assert carried[0].payload["carried_forward"] is True
    assert _events(nav, b, "run.rebased_needs_audit") == []
    assert [g.actor for g in _audits(nav, b)] == [AUDITOR, AUDITOR]


def test_forged_audit_event_without_grant_is_not_carried(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    nav.act.emit_event(Event(kind="run.audited", correlation_id=b.correlation_id,
                             payload={"run_id": b.id, "verified_sha": b.verified_sha,
                                      "actor": AUDITOR}))
    assert not is_audited(nav.act, b)
    _merge(nav, a)
    sb = nav.act.runs[b.id]
    assert sb.phase == RunPhase.awaiting_review and sb.verified_sha != b.verified_sha
    assert is_audited(nav.act, sb) is False
    assert _events(nav, b, "run.rebased_needs_audit")[-1].payload["reason"] == "no prior audit"
    assert not [e for e in _events(nav, b, "run.audited")
                if e.payload.get("verified_sha") == sb.verified_sha]
    assert _audits(nav, b) == []


def test_conflict_fails_run_and_grants_exactly_one_rebuild(tmp_path):
    builds = {"A": [("shared.py", "X = 1\n")], "B": [("shared.py", "X = 2\n")]}
    nav, repo, a, b = _setup(tmp_path, builds)
    done = _merge(nav, a)
    assert {r.id for r in done} == {a.id, b.id}
    head = _head(repo)
    sb = nav.act.runs[b.id]
    assert sb.status == RunStatus.failed and sb.phase == RunPhase.finalizing
    assert any("rebase conflict" in note for _, note, _ in nav.orc.updates)
    assert Path(sb.worktree_path).is_dir() and _clean(sb.worktree_path)
    assert _head(sb.worktree_path) == b.verified_sha  # rebase aborted, nothing moved
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]
    assert _events(nav, b, "run.rebuild_scheduled")[-1].payload["granted"] is True
    new = nav.approval_poll()
    assert [r.proposal_id for r in new] == [b.proposal_id]
    assert new[0].base_commit_sha == head and new[0].phase == RunPhase.awaiting_review
    assert nav.approval_poll() == []  # the grant bought exactly one rebuild
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]


def test_reverify_failure_fails_run_with_one_grant(tmp_path):
    # B's recorded command checks lib.py; A rewrites lib.py into something that no
    # longer passes that check (A itself only verifies a.py)
    builds = {"A": [("a.py", "A = 1\n"), ("lib.py", "def broken(:\n")],
              "B": [("b.py", "B = 1\n")]}
    verifies = {"A": [PY + ["a.py"]], "B": [PY + ["b.py"], PY + ["lib.py"]]}
    nav, repo, a, b = _setup(tmp_path, builds, verifies)
    assert [e["argv_list"] for e in b.verification] == verifies["B"]
    done = _merge(nav, a)
    assert {r.id for r in done} == {a.id, b.id}
    sb = nav.act.runs[b.id]
    assert sb.status == RunStatus.failed and sb.phase == RunPhase.finalizing
    assert any("re-verify failed" in note for _, note, _ in nav.orc.updates)
    assert Path(sb.worktree_path).is_dir()
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]
    assert _events(nav, b, "run.rebuild_scheduled")[-1].payload["granted"] is True


def test_flag_off_leaves_sibling_untouched(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    nav.settings.rebase_stale_runs = False
    before = len(_events(nav, b))
    assert [r.id for r in _merge(nav, a)] == [a.id]
    sb = nav.act.runs[b.id]
    assert (sb.base_commit_sha, sb.verified_sha) == (b.base_commit_sha, b.verified_sha)
    assert sb.phase == RunPhase.awaiting_review and sb.status == RunStatus.executing
    assert len(_events(nav, b)) == before
    assert _grants(nav, b) == []


def test_moved_branch_is_skipped_not_rebased(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    _git(b.worktree_path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "moved after verification")
    moved = _head(b.worktree_path)
    assert [r.id for r in _merge(nav, a)] == [a.id]
    sb = nav.act.runs[b.id]
    assert (sb.base_commit_sha, sb.verified_sha) == (b.base_commit_sha, b.verified_sha)
    assert sb.status == RunStatus.executing and sb.phase == RunPhase.awaiting_review
    assert _head(b.worktree_path) == moved
    assert "branch changed" in _events(nav, b, "run.rebase_skipped")[-1].payload["reason"]
    assert len(_events(nav, b, "run.awaiting_review")) == 1
    assert _grants(nav, b) == []


def test_rebase_makes_no_driver_calls(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    calls = nav.driver.calls
    assert [r.id for r in _merge(nav, a)] == [a.id]
    assert nav.act.runs[b.id].verified_sha != b.verified_sha  # it really was rebased
    assert nav.driver.calls == calls


def test_squash_repo_rebases_sibling_onto_squash_commit(tmp_path):
    nav, repo, a, b = _setup(tmp_path, squash=True)
    _git(repo, "config", "user.name", "Owner")
    _git(repo, "config", "user.email", "owner@users.noreply.github.com")
    before = _head(repo)
    assert [r.id for r in _merge(nav, a)] == [a.id]
    assert len(_git(repo, "rev-list", f"{before}..HEAD").split()) == 1
    head = _head(repo)
    sb = nav.act.runs[b.id]
    assert sb.phase == RunPhase.awaiting_review and sb.base_commit_sha == head
    assert sb.verified_sha == _head(sb.worktree_path) and sb.verified_sha != b.verified_sha
    _git(sb.worktree_path, "merge-base", "--is-ancestor", head, "HEAD")  # exits 0
    assert sb.verification and all(e["returncode"] == 0 for e in sb.verification)
    assert _events(nav, b, "run.awaiting_review")[-1].payload["rebased_from"] == b.verified_sha


def test_coarse_event_timestamps_still_carry_the_audit(tmp_path, monkeypatch):
    nav, repo, a, b = _setup(tmp_path)
    orig = nav.act.emit_event

    def coarse(event):
        event.ts = event.ts.replace(microsecond=0)  # ACT storing whole seconds
        return orig(event)

    monkeypatch.setattr(nav.act, "emit_event", coarse)
    ex._wait_clock_advance(datetime.now(timezone.utc))
    assert _audited(nav.act, [b.id], io.StringIO()) == 0
    assert is_audited(nav.act, b)
    _merge(nav, a)
    sb = nav.act.runs[b.id]
    assert sb.verified_sha != b.verified_sha
    carried = [e for e in _events(nav, b, "run.audited")
               if e.payload.get("verified_sha") == sb.verified_sha]
    assert len(carried) == 1 and carried[0].ts.microsecond == 0
    assert is_audited(nav.act, sb) is True
