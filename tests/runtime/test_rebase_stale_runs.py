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
from navigator.contracts.run import Run
from navigator.runtime import executor as ex
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.loop import Navigator
from navigator.safety.command_runner import CommandRunner
from structlog.testing import capture_logs

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


def _setup(tmp_path, builds=DEFAULT_BUILDS, verifies=None, squash=False,
           extra_base: dict[str, str] | None = None):
    """Two approved proposals A and B, built from one base in a single approval_poll.
    `extra_base` files (name -> content) are written and included in the base commit."""
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "lib.py").write_text("KEEP = 1\n")
    _git(repo, "add", "lib.py")
    for name, content in (extra_base or {}).items():
        (repo / name).write_text(content)
        _git(repo, "add", name)
    _git(repo, "commit", "-m", "base")
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


# ---- N12e: gap tests ---------------------------------------------------------------

def test_dirty_worktree_sibling_is_skipped_with_reason(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    Path(b.worktree_path, "lib.py").write_text("KEEP = 2\n")  # tracked file, not committed
    assert [r.id for r in _merge(nav, a)] == [a.id]
    sb = nav.act.runs[b.id]
    assert (sb.base_commit_sha, sb.verified_sha) == (b.base_commit_sha, b.verified_sha)
    assert sb.status == RunStatus.executing and sb.phase == RunPhase.awaiting_review
    assert _head(b.worktree_path) == b.verified_sha
    # the operator's edit is preserved: no abort/checkout touched it
    assert Path(b.worktree_path, "lib.py").read_text() == "KEEP = 2\n"
    ev = _events(nav, b, "run.rebase_skipped")[-1].payload
    assert ev["reason"] == "worktree has uncommitted changes" and ev["merged_run_id"] == a.id
    assert len(_events(nav, b, "run.awaiting_review")) == 1
    assert _grants(nav, b) == []
    assert sb.verification == b.verification


def test_sibling_of_another_repo_is_untouched(tmp_path):
    nav, repo, a, b = _setup(tmp_path)
    other = tmp_path / "other"; other.mkdir()
    _git(other, "init", "-b", "main")
    _git(other, "config", "user.email", "t@t"); _git(other, "config", "user.name", "t")
    (other / "o.py").write_text("O = 1\n")
    _git(other, "add", "o.py"); _git(other, "commit", "-m", "base")
    oh = _head(other)
    fp = nav.act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash="hash-F", title="F", rationale="r",
        repo=str(other), base_commit_sha=oh, intended_changes="F", affected_files=["o.py"]))
    foreign = nav.act.create_run(Run(
        proposal_id=fp.id, phase=RunPhase.awaiting_review, base_commit_sha=oh, branch="main",
        worktree_path=str(other), verified_sha=oh, correlation_id="corr-F"))
    before = len(_events(nav, foreign))
    assert [r.id for r in _merge(nav, a)] == [a.id]
    sb = nav.act.runs[b.id]
    assert sb.verified_sha != b.verified_sha  # the same-repo sibling WAS rebased
    sf = nav.act.runs[foreign.id]
    assert (sf.base_commit_sha, sf.verified_sha, sf.phase, sf.status) == (
        oh, oh, RunPhase.awaiting_review, RunStatus.executing)
    assert len(_events(nav, foreign)) == before
    assert _head(other) == oh and _clean(other)
    assert _grants(nav, foreign) == []


_CONFLICT_BUILDS = {"A": [("shared.py", "X = 1\n")], "B": [("shared.py", "X = 2\n")],
                    "C": [("c.py", "C = 1\n")]}


def _assert_c_rebased(nav, repo, c):
    sc = nav.act.runs[c.id]
    assert sc.phase == RunPhase.awaiting_review and sc.status == RunStatus.executing
    assert sc.verified_sha != c.verified_sha and sc.base_commit_sha == _head(repo)


def _assert_one_rebuild(nav, repo, b):
    new = nav.approval_poll()
    assert [r.proposal_id for r in new] == [b.proposal_id]
    assert new[0].base_commit_sha == _head(repo)
    assert nav.approval_poll() == []


def test_fail_stale_raising_on_conflict_keeps_the_loop_going(tmp_path):
    nav, repo, a, b = _setup(tmp_path, _CONFLICT_BUILDS)
    c = next(r for r in nav.act.list_open_runs() if r.id not in (a.id, b.id))
    real = nav.act.update_run
    raised = []

    def once(r):
        if r.id == b.id and r.status == RunStatus.failed and not raised:
            raised.append(r.id)
            raise RuntimeError(f"boom {b.worktree_path}")
        real(r)

    nav.act.update_run = once
    with capture_logs() as logs:
        done = _merge(nav, a)  # must not raise
    assert {r.id for r in done} == {a.id, b.id}
    assert raised == [b.id]
    _assert_c_rebased(nav, repo, c)
    sb = nav.act.runs[b.id]
    assert sb.status == RunStatus.failed and sb.phase == RunPhase.finalizing
    assert _head(b.worktree_path) == b.verified_sha and _clean(b.worktree_path)
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]
    ev = _events(nav, b, "run.rebuild_scheduled")[-1].payload
    assert ev["recovered_from"] == "RuntimeError" and ev["granted"] is True
    assert _events(nav, b, "run.rebase_skipped") == []
    entries = [l for l in logs if l["event"] == "rebase_stale.run_failed"]
    assert len(entries) == 1
    assert entries[0]["run_id"] == b.id and entries[0]["error"] == "RuntimeError"
    assert not any(l["event"] == "rebase_stale.cleanup_failed" for l in logs)
    for entry in logs:
        assert "boom" not in repr(entry) and b.worktree_path not in repr(entry)
    assert not any("boom" in repr(e.payload) for e in nav.act.events)
    _assert_one_rebuild(nav, repo, b)


def test_fail_stale_raising_in_outcome_write_still_grants_one_rebuild(tmp_path):
    nav, repo, a, b = _setup(tmp_path, _CONFLICT_BUILDS)
    c = next(r for r in nav.act.list_open_runs() if r.id not in (a.id, b.id))
    real = nav.orc.apply_memory_update

    def boom(section, note, replace=False):
        if b.id in note:
            raise RuntimeError("orc down")
        real(section, note, replace)

    nav.orc.apply_memory_update = boom
    done = _merge(nav, a)
    assert {r.id for r in done} == {a.id, b.id}
    sb = nav.act.runs[b.id]
    assert sb.status == RunStatus.failed
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]
    assert _events(nav, b, "run.rebuild_scheduled")[-1].payload["recovered_from"] == "RuntimeError"
    assert _events(nav, b, "run.failed") == []  # never reached, not replayed
    assert not any(b.id in note for _, note, _ in nav.orc.updates)
    _assert_c_rebased(nav, repo, c)
    _assert_one_rebuild(nav, repo, b)


def test_rebuild_grant_is_never_doubled_when_bookkeeping_runs_twice(tmp_path):
    builds = {"A": [("shared.py", "X = 1\n")], "B": [("shared.py", "X = 2\n")]}
    nav, repo, a, b = _setup(tmp_path, builds)
    real = nav.act.emit_event
    fired = []

    def once(ev):
        if (ev.kind == "run.rebuild_scheduled" and ev.payload.get("run_id") == b.id
                and not fired):
            fired.append(1)
            raise RuntimeError("act down")
        real(ev)

    nav.act.emit_event = once
    done = _merge(nav, a)
    assert {r.id for r in done} == {a.id, b.id}
    # the grant was created BEFORE the raise; the recovery must not add another
    assert [g.actor for g in _grants(nav, b)] == [ACTOR]
    ev = _events(nav, b, "run.rebuild_scheduled")
    assert len(ev) == 1 and ev[-1].payload["recovered_from"] == "RuntimeError"
    assert ev[-1].payload["granted"] is False  # the grant already existed
    assert ex._grant_rebuild(nav.act, nav.act.runs[b.id]) is False
    assert len(_grants(nav, b)) == 1
    new = nav.approval_poll()
    assert [r.proposal_id for r in new] == [b.proposal_id]
    assert nav.approval_poll() == []
    assert len(nav.act.list_runs_for_proposal(b.proposal_id)) == 2
    assert len(_grants(nav, b)) == 1


def test_whitespace_only_diff_change_does_not_carry_the_audit(tmp_path):
    wide = "".join(f"L{i} = {i}\n" for i in range(1, 41))
    # one trailing space on line 38: inside the 3 context lines of B's hunk, two
    # untouched lines away from B's insertion so git rebases cleanly
    a_wide = wide.replace("L38 = 38\n", "L38 = 38 \n")
    builds = {"A": [("wide.py", a_wide)], "B": [("wide.py", wide + "B = 1\n")]}
    nav, repo, a, b = _setup(tmp_path, builds, extra_base={"wide.py": wide})
    before = ex._normalized_diff(b.worktree_path, b.base_commit_sha)
    assert "B = 1" in before
    assert _audited(nav.act, [b.id, "--note", "x"], io.StringIO()) == 0
    assert is_audited(nav.act, b)
    assert [r.id for r in _merge(nav, a)] == [a.id]  # B rebased cleanly, not failed
    sb = nav.act.runs[b.id]
    assert sb.phase == RunPhase.awaiting_review and sb.verified_sha != b.verified_sha
    assert sb.base_commit_sha == _head(repo)
    after = ex._normalized_diff(sb.worktree_path, sb.base_commit_sha)
    assert after != before and after.split() == before.split()  # whitespace only
    assert is_audited(nav.act, sb) is False
    assert (_events(nav, b, "run.rebased_needs_audit")[-1].payload["reason"]
            == "diff changed on rebase")
    assert [e for e in _events(nav, b, "run.audited")
            if e.payload.get("verified_sha") == sb.verified_sha] == []
    assert [g.actor for g in _audits(nav, b)] == [AUDITOR]  # only the original audit
    assert _grants(nav, b) == []
    assert Path(sb.worktree_path, "wide.py").read_text() == a_wide + "B = 1\n"
