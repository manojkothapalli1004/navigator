"""Triage (operator 2026-10-03): retire repeats of done work, rank the rest with the
planner model (learning from the operator's own decisions), show only the top 3."""
import itertools
import json
import threading
from datetime import datetime, timedelta, timezone

from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ProposalStatus
from navigator.contracts.proposal import Proposal
from navigator.runtime import triage
from navigator.runtime.triage import Triage, codes, format_top, retire_candidates

_seq = itertools.count(1)
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _prop(act, title, status=ProposalStatus.proposed, repo="/r/navigator", rationale="r"):
    n = next(_seq)
    p = act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash=f"h-{n}", title=title, rationale=rationale,
        repo=repo, base_commit_sha="abc", intended_changes="x", affected_files=["a.py"],
        created_at=T0 + timedelta(minutes=n)))
    if status != ProposalStatus.proposed:
        act.set_proposal_status(p.id, status)
    return p


def _st(act, p):
    return act.get_proposal(p.id).status


def _triage(act, tmp_path, query_fn=None, now=None):
    rec = _Recorder()
    t = Triage(act, query_fn=query_fn, notifier=rec, path=tmp_path / "triage.json",
               now=now or (lambda: T0 + timedelta(days=1)))
    return t, rec


def test_codes_found_anywhere_in_title():
    assert codes("N5b: Telegram inbox") == {"N5b"}
    assert codes("Correct README docs (C4b)") == {"C4b"}
    assert codes("G1e-1d: resolved skip detector") == {"G1e-1d"}
    assert codes("Add real-FCM PushSender behind a config flag") == set()


def test_later_revision_covers_earlier_but_not_longer_numbers():
    done = {"C4c", "G1c", "N10", "N5b-2"}
    assert triage.covered_by("C4b", done) == "C4c"
    assert triage.covered_by("G1", done) == "G1c"
    assert triage.covered_by("N1", done) is None
    assert triage.covered_by("C4d", done) is None
    assert triage.covered_by("N5b-2", done) == "N5b-2"


def test_retires_repeats_of_done_work_and_older_open_twins():
    act = FakeACTClient()
    _prop(act, "G1: Verification fails when a run adds test skips", ProposalStatus.approved)
    _prop(act, "Add ping history endpoint with allowlist snapshot", ProposalStatus.approved,
          repo="/r/waker")
    a = _prop(act, "Fail runs that add test skips (G1) - cold audit")
    b = _prop(act, "Add ping history endpoint with allowlist-state snapshot tests",
              repo="/r/waker")
    old = _prop(act, "Add tests for storage builder_jobs and prompts persistence", repo="/r/orc")
    new = _prop(act, "Add storage tests for builder_jobs.py and prompts.py persistence",
                repo="/r/orc")
    keep = _prop(act, "N1: Smarter retry")
    retired = {p.id: why for p, why in retire_candidates(
        act.list_proposals(ProposalStatus.proposed), act.list_proposals(ProposalStatus.approved))}
    assert set(retired) == {a.id, b.id, old.id}
    assert "G1 already done" in retired[a.id]
    assert new.id not in retired and keep.id not in retired


def test_same_title_in_another_repo_is_not_a_repeat():
    act = FakeACTClient()
    _prop(act, "Add security helper tests for backend security", ProposalStatus.approved,
          repo="/r/waker")
    p = _prop(act, "Add security helper tests for backend security", repo="/r/orc")
    assert retire_candidates([act.get_proposal(p.id)],
                             act.list_proposals(ProposalStatus.approved)) == []


def test_run_supersedes_ranks_with_model_and_pushes_top3_once(tmp_path):
    act = FakeACTClient()
    _prop(act, "G2b: context allowance", ProposalStatus.approved)
    _prop(act, "Add flaky docs tweak", ProposalStatus.declined)
    dup = _prop(act, "G2b: give named files their own allowance")
    n1, n2, n3 = _prop(act, "N1: retry"), _prop(act, "N2: lessons"), _prop(act, "N3: audit")
    junk = _prop(act, "Add more docs tweaks")
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return json.dumps({"ranked": [{"id": n2.id, "why": "learning"},
                                      {"id": n1.id, "why": "retries"},
                                      {"id": n3.id, "why": "audit"}],
                           "drop": [{"id": junk.id, "why": "operator declines docs tweaks"}]})

    t, rec = _triage(act, tmp_path, model)
    out = t.run()
    assert _st(act, dup) == ProposalStatus.superseded
    assert _st(act, junk) == ProposalStatus.declined
    assert out["triage_source"] == "model" and out["triage_open"] == 3
    # the model learns from the operator's decisions and the priorities
    ctx = json.loads(prompts[0].split("context: ", 1)[1].split("\n", 1)[0])
    assert {"title": "Add flaky docs tweak", "decision": "declined"} in ctx["operator_past_decisions"]
    assert "Navigator is the #1 priority" in ctx["operator_priorities"]
    assert dup.id not in json.dumps(ctx["open_proposals"])
    top = rec.sent[-1]
    assert top.index("N2: lessons") < top.index("N1: retry") < top.index("N3: audit")
    assert "why: learning" in top and "retired 2" in rec.sent[0]
    # same open set: no model call, no repeat message
    rec.sent.clear()
    t.run()
    assert len(prompts) == 1 and rec.sent == []


def test_model_failure_falls_back_to_milestones_first(tmp_path):
    act = FakeACTClient()
    plain = _prop(act, "Add device-token endpoint")
    n1 = _prop(act, "N1: retry")
    n2 = _prop(act, "N2: lessons")

    def broken(prompt):
        raise RuntimeError("usage limit")

    t, rec = _triage(act, tmp_path, broken)
    assert t.run()["triage_source"] == "fallback"
    order = [p.id for p, _ in triage.ordered(act.list_proposals(ProposalStatus.proposed),
                                             tmp_path / "triage.json")]
    assert order == [n2.id, n1.id, plain.id]


def test_model_cannot_drop_unknown_ids_or_too_many(tmp_path):
    act = FakeACTClient()
    props = [_prop(act, f"Idea number {i} about thing{i}") for i in range(15)]

    def model(prompt):
        return json.dumps({"ranked": [], "drop": [{"id": p.id, "why": "x"} for p in props]
                           + [{"id": "proposal-nope", "why": "x"}]})

    t, _ = _triage(act, tmp_path, model)
    t.run()
    declined = [p for p in props if _st(act, p) == ProposalStatus.declined]
    assert len(declined) == triage.MAX_DROPS_PER_PASS


def test_reranks_when_open_set_changes_or_after_a_day(tmp_path):
    act = FakeACTClient()
    _prop(act, "N1: retry")
    calls = []
    clock = [T0 + timedelta(days=1)]

    def model(prompt):
        calls.append(1)
        return json.dumps({"ranked": [], "drop": []})

    t, _ = _triage(act, tmp_path, model, now=lambda: clock[0])
    t.run(); t.run()
    assert len(calls) == 1
    _prop(act, "N2: lessons")
    t.run()
    assert len(calls) == 2
    clock[0] += timedelta(days=2)
    t.run()
    assert len(calls) == 3


def test_format_top_pages_three_at_a_time():
    act = FakeACTClient()
    order = [(_prop(act, f"P{i}: x"), "") for i in range(7)]
    first = format_top(order)
    assert "top 1-3 of 7" in first and "4." not in first and "more" in first
    last = format_top(order, start=6)
    assert "top 7-7 of 7" in last and "more" not in last
    assert "nothing waiting" in format_top([])


# ---- triage.json cursor: locked, atomic, never regresses -----------------------------

def test_set_cursor_is_serialized_and_written_atomically(tmp_path):
    path = tmp_path / "triage.json"
    triage.set_cursor(path, 3)
    assert triage.load(path)["cursor"] == 3
    assert triage.lock_path(path) == tmp_path / "triage.json.lock"
    assert triage.lock_path(path).exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".triage.")] == []


def test_advance_cursor_never_moves_backwards_but_set_cursor_may(tmp_path):
    path = tmp_path / "triage.json"
    assert triage.advance_cursor(path, 6) == 6
    assert triage.advance_cursor(path, 3) == 6
    assert triage.load(path)["cursor"] == 6
    triage.set_cursor(path, 3)  # the operator restarted paging ('status')
    assert triage.load(path)["cursor"] == 3


def test_update_keeps_the_cursor_a_stale_writer_would_drop_or_lower(tmp_path):
    path = tmp_path / "triage.json"
    triage.set_cursor(path, 6)
    stale = {"ranked": [], "source": "fallback"}  # built from an old load: no cursor
    assert triage.update(path, lambda s: dict(stale))["cursor"] == 6
    assert triage.update(path, lambda s: {**s, "cursor": 2})["cursor"] == 6
    assert triage.update(path, lambda s: {**s, "cursor": 2},
                         rewind_cursor=True)["cursor"] == 2
    state = triage.load(path)
    assert state["ranked"] == [] and state["cursor"] == 2


def test_interleaved_advances_from_many_threads_end_at_the_highest(tmp_path):
    path = tmp_path / "triage.json"
    results = []

    def worker(start):
        results.append((start, triage.advance_cursor(path, start)))

    threads = [threading.Thread(target=worker, args=(3 * i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 20
    assert all(stored >= start for start, stored in results)
    assert triage.load(path)["cursor"] == 60


def test_cursor_written_while_the_daemon_ranks_survives_the_rerank(tmp_path):
    act = FakeACTClient()
    _prop(act, "N1: retry")
    _prop(act, "N2: lessons")
    path = tmp_path / "triage.json"

    def model(prompt):
        triage.set_cursor(path, 6)  # the reader answers 'more' while the model runs
        return json.dumps({"ranked": [], "drop": []})

    t, rec = _triage(act, tmp_path, model)
    t.run()
    state = triage.load(path)
    assert state["cursor"] == 6 and len(state["ranked"]) == 2 and state["top_sent"]
    t.run()  # cached pass: the daemon's later saves never lower it either
    assert triage.load(path)["cursor"] == 6
