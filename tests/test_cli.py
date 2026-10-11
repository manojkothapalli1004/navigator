import io
import subprocess
from navigator.cli import run_cli
from navigator.runtime.loop import Navigator
from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.safety.command_runner import CommandRunner
from navigator.config import Settings
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
