"""Builds (stage 2, part 1): filing a proposal through the ACT client and watching it to the end."""
import json
import logging
import threading

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.conductor import builds as B
from navigator.contracts.enums import ProposalStatus, RunPhase, RunStatus
from navigator.contracts.run import Run

SPEC = "x" * 500


class Cfg:
    tier_c_paths = ()
    tier_floor = "A"


def mk(tmp_path, act=None, settings=object(), finish=None, loop=None):
    act = act or FakeACTClient()
    got = [] if finish is None else finish
    b = B.Builds(tmp_path / "builds.json", act, settings=settings, on_finish=got.append,
                 base_sha_fn=lambda repo: "abc123", loop=loop)
    return b, act, got


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: Cfg() if s is not None else None)


def run_for(act, pid, status=RunStatus.executing, phase=RunPhase.building):
    r = Run(proposal_id=pid, base_commit_sha="a", branch="b", status=status, phase=phase)
    act.runs[r.id] = r
    return r


def test_file_build_shape_and_refusals():
    act = FakeACTClient()
    p = B.file_build(act, "navigator", "[draft] T", SPEC, files=["navigator/a.py"],
                     base_sha_fn=lambda r: "s")
    assert p.origin == "operator" and p.repo == B.BUILD_REPOS["navigator"]
    assert p.intended_changes == SPEC and p.affected_files == ["navigator/a.py"]
    assert p.title == "[draft] T" and p.base_commit_sha == "s" and p.id in act.proposals
    q = B.file_build(act, "navigator", "T2", SPEC, base_sha_fn=lambda r: "s")
    assert q.title == "[draft] T2"
    n = len(act.proposals)
    for proj, title, task, files in [("trader", "t", SPEC, []), ("nope", "t", SPEC, []),
                                     ("navigator", "", SPEC, []), ("navigator", "t", "short", []),
                                     ("navigator", "t", SPEC, ["/abs.py"]),
                                     ("navigator", "t", SPEC, ["a/../b.py"])]:
        with pytest.raises(B.BuildRefused):
            B.file_build(act, proj, title, task, files=files, base_sha_fn=lambda r: "s")
    assert len(act.proposals) == n
    assert "trader" not in B.BUILD_REPOS


def test_tier1_auto_approves_docs_only(tmp_path):
    b, act, _ = mk(tmp_path)
    rec = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")
    p = act.proposals[rec["proposal_id"]]
    assert p.status == ProposalStatus.approved and rec["tier1"] and p.title == "[draft] Doc"
    assert rec["title"] == "Doc"


@pytest.mark.parametrize("files,title", [(["navigator/a.py"], "T"), ([], "T"), (["docs/a.md"], "[draft] T")])
def test_not_tier1_stays_proposed(tmp_path, files, title):
    b, act, _ = mk(tmp_path)
    rec = b.file("navigator", title, SPEC, files, via="web")
    p = act.proposals[rec["proposal_id"]]
    assert p.status == ProposalStatus.proposed and not rec["tier1"]
    assert p.title.startswith("[draft] ") and rec["title"] == "T"


def test_no_repo_config_not_tier1(tmp_path):
    b, act, _ = mk(tmp_path, settings=None)
    rec = b.file("navigator", "T", SPEC, ["docs/a.md"], via="web")
    assert act.proposals[rec["proposal_id"]].status == ProposalStatus.proposed


def test_autoapprove_error_leaves_proposed(tmp_path, caplog):
    b, act, _ = mk(tmp_path)

    def boom(*a):
        raise RuntimeError("secret-text")
    act.set_proposal_status = boom
    with caplog.at_level(logging.WARNING):
        rec = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")
    p = act.proposals[rec["proposal_id"]]
    assert p.status == ProposalStatus.proposed and p.title == "[draft] Doc"  # daemon preapprove skips it
    assert not rec["tier1"]
    assert "conductor.build_autoapprove_failed: RuntimeError" in caplog.text
    assert "secret-text" not in caplog.text


def test_poll_states_and_dedupe(tmp_path):
    b, act, got = mk(tmp_path)
    rec = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")
    pid = rec["proposal_id"]
    b.poll()
    assert b.snapshot()[0]["state"] == "filed" and not got
    r = run_for(act, pid)
    b.poll()
    assert b.snapshot()[0]["state"] == "running" and not got
    act.runs[r.id].phase = RunPhase.awaiting_review
    b.poll()
    assert b.snapshot()[0]["state"] == "review" and len(got) == 1
    assert "ready for your merge OK" in got[0]["summary"]
    b.poll()
    assert len(got) == 1
    act.runs[r.id].status = RunStatus.completed
    b.poll()
    b.poll()
    assert b.snapshot()[0]["state"] == "done" and len(got) == 2 and not b.open()


def test_failed_stays_open_while_approved_then_closed(tmp_path):
    b, act, got = mk(tmp_path)
    pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
    r = run_for(act, pid, status=RunStatus.rolled_back)
    b.poll(); b.poll()
    assert b.snapshot()[0]["state"] == "failed" and len(got) == 1 and b.open()
    r2 = run_for(act, pid)
    r2.started_at = r.started_at.replace(year=r.started_at.year + 1)
    b.poll()
    assert b.snapshot()[0]["state"] == "running"
    act.proposals[pid].status = ProposalStatus.declined
    b.poll()
    assert b.snapshot()[0]["state"] == "closed" and not b.open() and len(got) == 2


def test_acting_error_in_poll(tmp_path, caplog):
    b, act, got = mk(tmp_path)
    b.file("navigator", "T", SPEC, ["navigator/a.py"], via="web")

    def boom(i):
        raise ValueError("body text")
    act.get_proposal = boom
    with caplog.at_level(logging.WARNING):
        b.poll()
    assert b.snapshot()[0]["state"] == "filed" and not got
    assert "conductor.build_poll_failed: ValueError" in caplog.text and "body text" not in caplog.text


def test_restart_keeps_open_and_reported(tmp_path):
    b, act, got = mk(tmp_path)
    pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
    r = run_for(act, pid)
    b.poll()
    b2, _, got2 = mk(tmp_path, act=act)
    assert b2.open()[0]["state"] == "running"
    act.runs[r.id].status = RunStatus.completed
    b2.poll()
    assert len(got2) == 1
    b3, _, got3 = mk(tmp_path, act=act)
    b3.poll()
    assert not got3 and not b3.open()


def test_stale_filed_reported_once(tmp_path):
    b, act, got = mk(tmp_path)
    b.file("navigator", "T", SPEC, ["navigator/a.py"], via="web")
    b._records[0]["filed"] -= B.BUILD_STALE_S + 5
    b.poll(); b.poll()
    assert len(got) == 1 and "still waiting for approval" in got[0]["summary"]
    assert b.open()


def test_stale_approved_no_run_says_dry_run(tmp_path):
    b, act, got = mk(tmp_path)
    b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")  # tier 1: auto-approved, no run
    b._records[0]["filed"] -= B.BUILD_STALE_S + 5
    b.poll(); b.poll()
    assert len(got) == 1
    assert "approved" in got[0]["summary"] and "dry-run" in got[0]["summary"]
    assert "still waiting for approval" not in got[0]["summary"]
    assert b.open()


def test_stale_key_unchanged(tmp_path):
    b, act, got = mk(tmp_path)
    pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
    b._records[0]["filed"] -= B.BUILD_STALE_S + 5
    b.poll()
    assert got[0]["key"] == f"{pid}:stale"


def test_secret_masked_and_reply_via(tmp_path):
    b, act, got = mk(tmp_path)
    pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="telegram:5")["proposal_id"]
    run_for(act, pid, status=RunStatus.failed)
    b.poll()
    assert got[0]["reply_via"] == "telegram:5" and got[0]["state"] == "failed"
    b2, act2, got2 = mk(tmp_path / "x")
    p2 = b2.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
    act2.proposals[p2].status = ProposalStatus.expired
    act2.proposals[p2].rationale = "n"
    assert B.mask("key sk-abcdefgh12345678 end").count("sk-abcdefgh") == 0


def test_poll_thread_and_on_finish_on_loop_thread(tmp_path):
    import asyncio

    async def go():
        loop = asyncio.get_running_loop()
        seen = []
        act = FakeACTClient()
        b = B.Builds(tmp_path / "b.json", act, settings=object(), on_finish=lambda r: seen.append(
            threading.get_ident()), base_sha_fn=lambda r: "a", loop=loop)
        pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
        run_for(act, pid, status=RunStatus.completed)
        await asyncio.to_thread(b.poll)
        await asyncio.sleep(0.05)
        assert seen == [threading.get_ident()]
    asyncio.run(go())


def test_concurrent_file_and_poll(tmp_path):
    b, act, _ = mk(tmp_path)
    ts = [threading.Thread(target=lambda: b.file("navigator", "T", SPEC, [], via="web")) for _ in range(8)]
    ts += [threading.Thread(target=b.poll) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(json.loads((tmp_path / "builds.json").read_text())["builds"]) == 8


def test_crash_between_queue_and_save_duplicates_not_loses(tmp_path):
    b, act, got = mk(tmp_path)
    pid = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")["proposal_id"]
    run_for(act, pid, status=RunStatus.completed)
    real = b._save

    def crash():
        raise OSError("disk")
    b._save = crash
    with pytest.raises(OSError):
        b.poll()
    assert len(got) == 1
    b2, _, got2 = mk(tmp_path, act=act)
    b2.poll()
    assert len(got2) == 1
    b2.poll()
    assert len(got2) == 1


# ---- build cards (D): the needs-approval callback, poll repair, decide
def test_needs_approval_callback_once_with_reason(tmp_path):
    b, act, _ = mk(tmp_path)
    seen = []
    b.on_needs_approval = seen.append
    b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")
    assert seen == []
    rec = b.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")
    (s,) = seen
    assert s["proposal_id"] == rec["proposal_id"] and "navigator/a.py" in s["approval_reason"]
    assert s["title"] == "Code" and s["files"] == ["navigator/a.py"]

    def boom(pid, status):
        raise RuntimeError("x")
    act.set_proposal_status = boom
    b.file("navigator", "Doc2", SPEC, ["docs/b.md"], via="web")
    assert seen[-1]["approval_reason"] == "auto-approve failed (RuntimeError); waiting for you"
    assert len(seen) == 2


def test_callback_error_does_not_fail_filing(tmp_path, caplog):
    b, act, _ = mk(tmp_path)

    def boom(rec):
        raise RuntimeError("secret text in message")
    b.on_needs_approval = boom
    with caplog.at_level(logging.WARNING):
        rec = b.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")
    assert rec["proposal_id"] in act.proposals
    assert "conductor.build_card_failed: RuntimeError" in caplog.text and "secret text" not in caplog.text


def test_poll_reraises_card_request_for_uncarded_waiting_build(tmp_path):
    b, act, _ = mk(tmp_path)

    def boom(rec):
        raise RuntimeError("x")
    b.on_needs_approval = boom
    pid = b.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")["proposal_id"]
    seen = []
    b.on_needs_approval = seen.append
    b.poll()
    assert [s["proposal_id"] for s in seen] == [pid]
    act.proposals[pid].status = ProposalStatus.approved
    b.poll()
    assert len(seen) == 1


def test_decide_idempotent_and_guarded(tmp_path):
    b, act, _ = mk(tmp_path)
    pid = b.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")["proposal_id"]
    b.decide(pid, True)
    assert act.proposals[pid].status == ProposalStatus.approved
    b.decide(pid, True)
    with pytest.raises(B.BuildRefused, match="proposal already approved"):
        b.decide(pid, False)
    with pytest.raises(B.BuildRefused):
        b.decide("nope", True)


def test_ledger_lines_per_state_change_not_per_poll(tmp_path):
    sink = []
    act = FakeACTClient()
    b = B.Builds(tmp_path / "builds.json", act, settings=object(), on_finish=lambda r: None,
                 base_sha_fn=lambda repo: "abc123", ledger=sink.append)
    rec = b.file("navigator", "T", SPEC, ["docs/a.md"], via=None)
    pid = rec["proposal_id"]
    b.poll(); b.poll()  # still filed: no new line
    r = run_for(act, pid)
    b.poll(); b.poll()
    r.phase = RunPhase.awaiting_review
    b.poll(); b.poll()
    r.status = RunStatus.completed
    b.poll(); b.poll()
    assert [(x["state"], x["prev"]) for x in sink] == [
        ("filed", None), ("running", "filed"), ("review", "running"), ("done", "review")]
    assert all(x["kind"] == "build" and x["id"] == pid and x["v"] == 1 for x in sink)
    b2 = B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda repo: "abc123",
                  ledger=sink.append)
    b2.poll()
    assert len(sink) == 4
