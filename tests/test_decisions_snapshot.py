"""U3: navigator/decisions snapshot doc and navigator/digest/latest + navigator.digest."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import navigator.__main__ as main_mod
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, ProposalStatus, RunPhase
from navigator.contracts.outcome import Outcome
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.observability import decisions, telegram_inbox
from navigator.runtime.outcome_store import OutcomeStore

NY = ZoneInfo("America/New_York")
AUDITED: set[str] = set()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_TZ", "America/New_York")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_STATE_PATH", str(tmp_path / "decisions_state.json"))
    monkeypatch.setenv("NAVIGATOR_MANUAL_DECISIONS_PATH", str(tmp_path / "manual_decisions.json"))
    monkeypatch.setenv("NAVIGATOR_OUTCOME_PATH", str(tmp_path / "outcomes.jsonl"))
    monkeypatch.delenv("NAVIGATOR_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("NAVIGATOR_DECISIONS_NUDGES", raising=False)
    AUDITED.clear()
    monkeypatch.setattr(telegram_inbox, "is_audited", lambda act, run_id: run_id in AUDITED)


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _ny(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


def _proposal(act, title, repo="/repo"):
    p = Proposal(origin="periodic", proposal_origin_hash=f"h-{title}", title=title,
                 rationale="r", repo=repo, base_commit_sha="abc", intended_changes="x",
                 affected_files=["a.py"])
    return act.create_proposal(p)


def _merge_run(act, title, audited=True, repo="/repo"):
    p = _proposal(act, title, repo=repo)
    act.set_proposal_status(p.id, ProposalStatus.approved)
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="abc", branch=f"nav/{p.id}",
                             phase=RunPhase.awaiting_review))
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    if audited:
        AUDITED.add(run.id)
    return run


def _seed(path, first_seen, digest):
    assert decisions.write_state(path, {
        "first_seen": {k: v.isoformat() for k, v in first_seen.items()},
        "nudged": {}, "digest": digest})


def _digest_events(act):
    return [e for e in act.events if e.kind == "navigator.digest"]


def _outcomes(n=0, statuses=None):
    store = OutcomeStore(decisions._outcome_path())
    out = []
    for i, st in enumerate(statuses or ["completed"] * n):
        o = Outcome(run_id=f"run-{i}", proposal_id=f"p-{i}", status=st, summary=f"s{i}")
        store.append(o)
        out.append(o)
    return out


# ---- snapshot -------------------------------------------------------------------

def test_snapshot_merge_item():
    act = FakeACTClient()
    run = _merge_run(act, "Quota: pause builds", repo="/the/repo")
    (approval,) = act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                     decision=ApprovalDecision.pending)
    body = decisions.snapshot(act)
    (item,) = body["items"]
    assert item["kind"] == "merge" and item["key"] == f"merge:{run.id}"
    assert item["actions"] == ["merge", "reject"]
    assert item["approval_id"] == approval.id
    assert item["run_id"] == run.id and item["proposal_id"] == run.proposal_id
    assert item["repo"] == "/the/repo"
    assert body["awaiting_audit"] == [] and body["failed"] == []


def test_snapshot_unaudited_run_is_awaiting_audit():
    act = FakeACTClient()
    run = _merge_run(act, "Later: not audited", audited=False)
    body = decisions.snapshot(act)
    assert body["items"] == []
    assert [a["run_id"] for a in body["awaiting_audit"]] == [run.id]
    assert body["awaiting_audit"][0]["title"] == "Later: not audited"
    assert body["awaiting_audit"][0]["proposal_id"] == run.proposal_id


def test_snapshot_proposal_item():
    act = FakeACTClient()
    p = _proposal(act, "N20: trim logs")
    (item,) = decisions.snapshot(act)["items"]
    assert item["kind"] == "proposal" and item["actions"] == ["approve", "decline"]
    assert item["proposal_id"] == p.id and item["repo"] == "/repo"
    assert item["since"] is not None


def test_snapshot_manual_item(tmp_path):
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install plist", "launchctl load ~/x.plist")
    (item,) = decisions.snapshot(FakeACTClient(), manual_file=str(manual))["items"]
    assert item["kind"] == "manual" and item["actions"] == []
    assert item["how"] == "launchctl load ~/x.plist"


def test_snapshot_source_failure_listed(tmp_path, monkeypatch):
    act = FakeACTClient()
    _merge_run(act, "Quota: pause builds")
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install plist", "launchctl load")

    def broken(*a, **k):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_proposals", broken)
    body = decisions.snapshot(act, manual_file=str(manual))
    assert "proposal" in body["failed"]
    assert [i["kind"] for i in body["items"]] == ["merge", "manual"]


# ---- B4: declined runs the operator may fix ------------------------------------------

SNAPSHOT_KEYS = {"key", "kind", "title", "reply", "since", "repo", "run_id", "proposal_id",
                 "approval_id", "tier", "actions", "how"}


def _declined_run(act, title, note=None, days_ago=0, repo="/repo", status=ProposalStatus.approved):
    from datetime import timezone
    from navigator.contracts.enums import RunStatus
    p = _proposal(act, title, repo=repo)
    act.set_proposal_status(p.id, status)
    ended = datetime.now(timezone.utc) - timedelta(days=days_ago)
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="abc", branch=f"nav/{p.id}",
                             status=RunStatus.aborted, phase=RunPhase.finalizing,
                             started_at=ended - timedelta(hours=1), ended_at=ended))
    appr = act.create_approval(target=run.id, gate=ApprovalGate.merge)
    act.decide_approval(appr.id, ApprovalDecision.declined, actor="operator-telegram",
                        **({"note": note} if note else {}))
    return run


def test_snapshot_declined_entry_shape_and_actions():
    act = FakeACTClient()
    run = _declined_run(act, "S3a: squash merges", note="reason=unsafe: auth", repo="/the/repo")
    merge_run = _merge_run(act, "Quota: pause builds")
    body = decisions.snapshot(act)
    assert decisions.KIND_ACTIONS["declined"] == ("fix",)
    (item,) = body["declined"]
    assert set(item) == SNAPSHOT_KEYS
    assert item == {"key": f"declined:{run.id}", "kind": "declined", "title": "S3a: squash merges",
                    "reply": "fix S3a", "since": run.ended_at.isoformat(), "repo": "/the/repo",
                    "run_id": run.id, "proposal_id": run.proposal_id, "approval_id": None,
                    "tier": None, "actions": ["fix"], "how": None}
    # NOT in items: the Telegram 'decisions' list, digest and nudges are unchanged
    assert [i["kind"] for i in body["items"]] == ["merge"]
    assert body["items"][0]["run_id"] == merge_run.id
    assert [d.kind for d in decisions.collect(act)] == ["merge"]
    assert body["failed"] == [] and body["awaiting_audit"] == []
    # a title that is a run-id-like or non-ASCII name replies with the run id
    act2 = FakeACTClient()
    odd = _declined_run(act2, "run-like: title")
    assert decisions.snapshot(act2)["declined"][0]["reply"] == f"fix {odd.id}"


def test_snapshot_declined_excludes_old_queued_and_non_fixable():
    act = FakeACTClient()
    fresh = _declined_run(act, "S3a: fresh")
    _declined_run(act, "Old: eight days", days_ago=8)
    # already granted a rebuild (fix_reason 'already queued'): no dead Fix button
    queued = _declined_run(act, "Q: queued")
    g = act.create_approval(target=queued.proposal_id, gate=ApprovalGate.review)
    act.decide_approval(g.id, ApprovalDecision.approved, actor="operator")
    # proposal no longer approved: fix would be refused, so it is not offered
    _declined_run(act, "D: declined proposal", status=ProposalStatus.declined)
    body = decisions.snapshot(act)
    assert [d["run_id"] for d in body["declined"]] == [fresh.id]
    assert body["items"] == [] and body["failed"] == []


def test_snapshot_declined_source_failure_is_listed(monkeypatch):
    act = FakeACTClient()
    _declined_run(act, "S3a: fresh")
    run = _merge_run(act, "Quota: pause builds")

    def broken(*a, **k):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_all_runs", broken)
    body = decisions.snapshot(act)
    assert body["declined"] == [] and "declined" in body["failed"]
    assert [i["run_id"] for i in body["items"]] == [run.id]  # the rest is intact
    assert "merge" not in body["failed"]


def test_snapshot_without_declines_has_an_empty_list():
    act = FakeACTClient()
    _merge_run(act, "Quota: pause builds")
    body = decisions.snapshot(act)
    assert body["declined"] == [] and body["failed"] == []
    assert set(body) == {"ts", "items", "awaiting_audit", "failed", "declined"}


# ---- the daemon step ------------------------------------------------------------

def test_decisions_step_writes_snapshot_doc():
    act = FakeACTClient()
    _merge_run(act, "Quota: pause builds")
    nav = SimpleNamespace(act=act, notifier=_Recorder())
    summary = {}
    main_mod._decisions_step(nav, summary)
    body, _version = act.get_doc("navigator/decisions")
    assert [i["key"] for i in body["items"]] == [d.key for d in
                                                 decisions.collect_with_status(act)[0]]
    assert len(body["items"]) == 1
    assert act.docs["navigator/decisions"][2] == "navigator-daemon"
    assert "decisions_snapshot_error" not in summary


def test_snapshot_runs_when_tick_raises(monkeypatch):
    def boom(act, notifier):
        raise RuntimeError("tick failed")

    monkeypatch.setattr(decisions, "decisions_tick", boom)
    act = FakeACTClient()
    nav = SimpleNamespace(act=act, notifier=_Recorder())
    summary = {}
    main_mod._decisions_step(nav, summary)
    assert summary["decisions_error"] == "RuntimeError"
    assert act.get_doc("navigator/decisions") is not None


def test_decisions_step_snapshot_error_reported(monkeypatch):
    result = {"items": 0, "digest_sent": False, "nudged": 0}
    monkeypatch.setattr(decisions, "decisions_tick", lambda act, notifier: result)
    act = FakeACTClient()

    def failing_put(key, body, actor, expected_version=None):
        raise OSError("disk full")

    monkeypatch.setattr(act, "put_doc", failing_put)
    nav = SimpleNamespace(act=act, notifier=_Recorder())
    summary = {}
    main_mod._decisions_step(nav, summary)
    assert summary["decisions_snapshot_error"] == "OSError"
    assert summary["decisions"] == result
    assert "decisions_error" not in summary


# ---- the digest -----------------------------------------------------------------

def _due_setup(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(25, 8)
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5)}, "2026-10-24")
    return act, rec, state, now


def test_digest_publishes_doc_and_event(tmp_path):
    act, rec, state, now = _due_setup(tmp_path)
    done, bad = _outcomes(statuses=["completed", "failed"])
    out = decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert out == {"items": 1, "digest_sent": True, "nudged": 0}
    body, _ = act.get_doc("navigator/digest/latest")
    assert body["date"] == "2026-10-25"
    assert body["chunks"] == rec.sent
    assert [m["run_id"] for m in body["merged_since_last"]] == [done.run_id]
    assert [f["run_id"] for f in body["failed_since_last"]] == [bad.run_id]
    assert body["outcomes_through"] == bad.id
    assert len(_digest_events(act)) == 1
    assert act.docs["navigator/digest/latest"][2] == "navigator-daemon"


def test_second_digest_only_new_outcomes(tmp_path):
    act, rec, state, now = _due_setup(tmp_path)
    x, a, b = _outcomes(3)
    act.put_doc("navigator/digest/latest", {"date": "2026-10-24", "outcomes_through": x.id},
                actor="navigator-daemon")
    decisions.decisions_tick(act, rec, now=now, state_file=state)
    body, _ = act.get_doc("navigator/digest/latest")
    assert [m["run_id"] for m in body["merged_since_last"]] == [b.run_id, a.run_id]
    assert body["outcomes_through"] == b.id


def test_outcomes_through_not_found_takes_newest_20(tmp_path):
    act, rec, state, now = _due_setup(tmp_path)
    made = _outcomes(30)
    act.put_doc("navigator/digest/latest",
                {"date": "2026-10-24", "outcomes_through": "outcome-missing"},
                actor="navigator-daemon")
    decisions.decisions_tick(act, rec, now=now, state_file=state)
    body, _ = act.get_doc("navigator/digest/latest")
    assert len(body["merged_since_last"]) == 20
    assert body["merged_since_last"][0]["run_id"] == made[-1].run_id


def test_redigest_same_day_does_not_overwrite(tmp_path):
    act, rec, state, now = _due_setup(tmp_path)
    prev = {"date": "2026-10-25", "marker": "first"}
    act.put_doc("navigator/digest/latest", prev, actor="navigator-daemon")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert out == {"items": 1, "digest_sent": True, "nudged": 0}
    assert act.get_doc("navigator/digest/latest") == (prev, 1)
    assert _digest_events(act) == []


def test_digest_not_due_publishes_nothing(tmp_path):
    act, rec, state, now = _due_setup(tmp_path)
    _seed(state, {}, "2026-10-25")
    decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert act.get_doc("navigator/digest/latest") is None
    assert _digest_events(act) == []


def test_digest_due_with_nothing_waiting_still_publishes(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    out = decisions.decisions_tick(act, rec, now=_ny(25, 9), state_file=state,
                                   manual_file=str(tmp_path / "none.json"))
    assert out == {"items": 0, "digest_sent": False, "nudged": 0}
    body, _ = act.get_doc("navigator/digest/latest")
    assert body["chunks"] == [] and body["items"] == []
    assert len(_digest_events(act)) == 1


def test_digest_publish_failure_keeps_marks(tmp_path, monkeypatch):
    act, rec, state, now = _due_setup(tmp_path)

    def failing_put(key, body, actor, expected_version=None):
        raise RuntimeError("ACT down")

    monkeypatch.setattr(act, "put_doc", failing_put)
    out = decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert out["digest_publish_error"] == "RuntimeError"
    assert out["digest_sent"] is True
    assert decisions.read_state(state)["digest"] == "2026-10-25"
    assert len(rec.sent) == 1 and rec.sent[0].startswith("Morning digest")
    assert _digest_events(act) == []
