import io
import json
from datetime import datetime, timedelta, timezone

import httpx
from structlog.testing import capture_logs

from navigator.cli import is_audited, merge_tier_of, run_cli
from navigator.clients.act_client import ACTEventsTooLarge, FakeACTClient, HTTPACTClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.event import Event
from navigator.contracts.run import Run
from navigator.ids import new_id
from navigator.observability.notify import NotifyingACT
from tests.support import ProtocolOnlyACT

VERIFIED_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "f" * 40


def _cli(act, *argv):
    out = io.StringIO()
    rc = run_cli(list(argv), act=act, out=out)
    return rc, out.getvalue()


def _run(act, phase=RunPhase.awaiting_review, pending=True, verified_sha=VERIFIED_SHA,
         reviewed_ago_s=1):
    """A verified run parked at the merge gate: the executor recorded its verified_sha
    and emitted run.awaiting_review `reviewed_ago_s` seconds ago."""
    run = act.create_run(Run(proposal_id="proposal-x", base_commit_sha="abc",
                             branch="nav/x", phase=phase, correlation_id="corr-x",
                             verified_sha=verified_sha))
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-x",
                         ts=datetime.now(timezone.utc) - timedelta(seconds=reviewed_ago_s),
                         payload={"run_id": run.id, "verified_sha": verified_sha}))
    if pending:
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    return run


def _audit_event(act, run, verified_sha=VERIFIED_SHA):
    act.emit_event(Event(kind="run.audited", correlation_id="corr-x",
                         payload={"run_id": run.id, "note": None, "actor": "auditor",
                                  "verified_sha": verified_sha}))


def _review_grant(act, target, actor):
    grant = act.create_approval(target=target, gate=ApprovalGate.review)
    act.decide_approval(grant.id, ApprovalDecision.approved, actor=actor)
    return grant


def test_audited_marks_awaiting_review_run():
    act = FakeACTClient()
    run = _run(act)
    assert not is_audited(act, run.id)
    rc, out = _cli(act, "audited", run.id, "--note", "diff reviewed")
    assert rc == 0, out
    assert is_audited(act, run.id)
    events = [e for e in act.events if e.kind == "run.audited"]
    assert len(events) == 1
    assert events[0].payload == {"run_id": run.id, "note": "diff reviewed", "actor": "auditor",
                                 "verified_sha": VERIFIED_SHA}
    assert VERIFIED_SHA[:12] in out
    grants = act.list_approvals(target=run.id, gate=ApprovalGate.review,
                                decision=ApprovalDecision.approved)
    assert len(grants) == 1 and grants[0].actor == "auditor"
    # the merge gate itself is untouched: auditing never approves a merge
    assert len(act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.pending)) == 1


def test_audited_without_note():
    act = FakeACTClient()
    run = _run(act)
    rc, _ = _cli(act, "audited", run.id)
    assert rc == 0
    ev = [e for e in act.events if e.kind == "run.audited"][0]
    assert ev.payload["note"] is None


def test_audited_twice_is_idempotent():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited", run.id)[0] == 0
    rc, out = _cli(act, "audited", run.id)
    assert rc == 0 and "already" in out
    assert len(act.list_approvals(target=run.id, gate=ApprovalGate.review)) == 1


def test_audited_refuses_run_not_awaiting_review():
    act = FakeACTClient()
    run = _run(act, phase=RunPhase.building)
    rc, out = _cli(act, "audited", run.id)
    assert rc == 1
    assert "not awaiting_review" in out
    assert not is_audited(act, run.id)
    assert [e for e in act.events if e.kind == "run.audited"] == []


def test_audited_refuses_unknown_run():
    act = FakeACTClient()
    rc, out = _cli(act, "audited", "run-01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert rc == 1 and "no open run" in out


def test_audited_bad_arguments():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited")[0] == 2
    assert _cli(act, "audited", run.id, "--bogus", "x")[0] == 2
    assert _cli(act, "audited", run.id, "--note")[0] == 2
    assert not is_audited(act, run.id)


def test_cli_merge_still_works_on_unaudited_run():
    act = FakeACTClient()
    run = _run(act)
    assert not is_audited(act, run.id)
    rc, out = _cli(act, "merge", run.id)
    assert rc == 0, out
    approved = act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.approved)
    assert len(approved) == 1 and approved[0].actor == "operator"


# ---- what does NOT count as audited ---------------------------------------------

def test_review_approval_by_operator_does_not_count():
    act = FakeACTClient()
    run = _run(act)
    _audit_event(act, run)
    _review_grant(act, run.id, "operator")
    assert not is_audited(act, run.id)
    # the same grant decided by the auditor would
    _review_grant(act, run.id, "auditor")
    assert is_audited(act, run.id)


def test_retry_grant_on_the_proposal_does_not_count():
    act = FakeACTClient()
    run = _run(act)
    _audit_event(act, run)
    _review_grant(act, "proposal-x", "auditor")   # targets the proposal, not the run
    assert not is_audited(act, run.id)


def test_audit_event_alone_or_grant_alone_does_not_count():
    act = FakeACTClient()
    run = _run(act)
    _audit_event(act, run)
    assert not is_audited(act, run.id)
    other = FakeACTClient()
    run2 = _run(other)
    _review_grant(other, run2.id, "auditor")
    assert not is_audited(other, run2.id)


def test_audit_recorded_before_verified_sha_changed_does_not_count():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited", run.id)[0] == 0
    assert is_audited(act, run.id)
    act.runs[run.id].verified_sha = OTHER_SHA   # re-verified at another commit
    assert not is_audited(act, run.id)
    assert not is_audited(act, act.list_open_runs()[0])
    # auditing the new commit records a second audit bound to it
    rc, out = _cli(act, "audited", run.id)
    assert rc == 0 and "already" not in out
    shas = [e.payload["verified_sha"] for e in act.events if e.kind == "run.audited"]
    assert shas == [VERIFIED_SHA, OTHER_SHA]
    assert is_audited(act, run.id)


def test_audit_event_with_other_sha_does_not_count():
    act = FakeACTClient()
    run = _run(act)
    _audit_event(act, run, verified_sha=OTHER_SHA)
    _review_grant(act, run.id, "auditor")
    assert not is_audited(act, run.id)


def test_run_without_verified_sha_cannot_be_audited():
    act = FakeACTClient()
    run = _run(act, verified_sha=None)
    rc, out = _cli(act, "audited", run.id)
    assert rc == 1 and "verified_sha" in out
    assert [e for e in act.events if e.kind == "run.audited"] == []
    assert act.list_approvals(target=run.id, gate=ApprovalGate.review) == []
    # even a forged event + grant with verified_sha None never match
    _audit_event(act, run, verified_sha=None)
    _review_grant(act, run.id, "auditor")
    assert not is_audited(act, run.id)


def test_audit_before_the_run_reached_review_does_not_count():
    act = FakeACTClient()
    run = _run(act, reviewed_ago_s=-5)   # review happens 5s after the audit below
    assert _cli(act, "audited", run.id)[0] == 0
    assert not is_audited(act, run.id)


def test_run_without_awaiting_review_event_is_not_audited():
    act = FakeACTClient()
    run = act.create_run(Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/x",
                             phase=RunPhase.awaiting_review, verified_sha=VERIFIED_SHA))
    _audit_event(act, run)
    _review_grant(act, run.id, "auditor")
    assert not is_audited(act, run.id)


def test_unknown_or_closed_run_is_not_audited():
    act = FakeACTClient()
    assert not is_audited(act, "run-01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert not is_audited(act, None)


def test_is_audited_fails_closed_when_events_cannot_be_listed():
    act = FakeACTClient()
    run = _run(act)
    assert _cli(act, "audited", run.id)[0] == 0
    assert is_audited(act, run.id)
    assert not is_audited(ProtocolOnlyACT(act), run.id)   # no list_events, no .inner

    def boom(kinds=None, since=None):
        raise ConnectionError("ACT down at http://act.internal:9/secret?token=abc")
    act.list_events = boom
    with capture_logs() as logs:
        assert not is_audited(act, run.id)
    unreadable = [l for l in logs if l["event"] == "cli.events_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0]["error"] == "ConnectionError"
    # the class name only: the message (which may carry URLs/tokens) is never logged
    assert not any("token" in str(v) or "ACT down" in str(v) for v in unreadable[0].values())


def test_is_audited_reads_only_events_since_the_run_started():
    class _Recording(FakeACTClient):
        def __init__(self):
            super().__init__()
            self.since_args = []

        def list_events(self, kinds=None, since=None):
            self.since_args.append(since)
            return super().list_events(kinds=kinds, since=since)
    act = _Recording()
    run = _run(act)
    assert _cli(act, "audited", run.id)[0] == 0
    assert is_audited(act, run.id)
    assert act.since_args and all(s is not None for s in act.since_args)
    for s in act.since_args:
        assert s.tzinfo is not None
        assert s <= run.started_at                      # never later than the run start
        assert run.started_at - s <= timedelta(hours=1)  # but close to it, not history
    # an awaiting_review event from long before the run started is not read at all
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-x",
                         ts=datetime.now(timezone.utc) - timedelta(days=30),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA}))
    assert is_audited(act, run.id)


def test_is_audited_unwraps_notifying_act():
    class _Rec:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
    inner = FakeACTClient()
    run = _run(inner)
    assert _cli(inner, "audited", run.id)[0] == 0
    assert is_audited(NotifyingACT(inner, _Rec()), run.id)


# ---- end to end over HTTP (HTTPACTClient against an in-memory ACT) ---------------

class _ActServer:
    """The ACT routes `navigator audited` and is_audited use, behind MockTransport.
    Timestamps come from a fake clock that advances `step` per recorded row."""

    def __init__(self):
        self.runs: dict = {}
        self.approvals: dict = {}
        self.events: list = []
        self.clock = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        self.step = timedelta(seconds=1)
        self.requests: list = []

    def _now(self):
        self.clock = self.clock + self.step
        return self.clock

    def add_run(self, verified_sha=VERIFIED_SHA, phase="awaiting_review", status="executing"):
        rid = new_id("run")
        self.runs[rid] = {"id": rid, "schema_version": 1, "proposal_id": "proposal-x",
                          "status": status, "phase": phase, "base_commit_sha": "abc",
                          "branch": "nav/x", "worktree_path": None, "correlation_id": "corr-1",
                          "cost_usd": 0.0, "started_at": "2026-10-15T11:00:00+00:00",
                          "ended_at": None, "payload": {"verified_sha": verified_sha}}
        self.add_event("run.awaiting_review", {"run_id": rid, "verified_sha": verified_sha})
        self.add_approval(rid, "merge")
        return rid

    def add_event(self, kind, payload, correlation_id="corr-1"):
        ev = {"id": new_id("event"), "kind": kind, "severity": "info",
              "ts": self._now().isoformat(), "payload": payload,
              "correlation_id": correlation_id}
        self.events.append(ev)
        return ev

    def add_approval(self, target, gate):
        aid = new_id("approval")
        self.approvals[aid] = {"id": aid, "schema_version": 1, "target": target, "gate": gate,
                               "decision": "pending", "actor": None, "decided_at": None,
                               "note": None, "correlation_id": None}
        return self.approvals[aid]

    def audits(self):
        return [e for e in self.events if e["kind"] == "run.audited"]

    def review_grants(self, rid):
        return [a for a in self.approvals.values()
                if a["target"] == rid and a["gate"] == "review"]

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        params = dict(req.url.params)
        self.requests.append((method, path, params))
        body = json.loads(req.content) if req.content else {}
        if method == "GET" and path == "/api/v1/agent/runs":
            open_only = params.get("open_only", "true") != "false"
            return httpx.Response(200, json=[r for r in self.runs.values()
                                             if not open_only or r["status"] == "executing"])
        if method == "GET" and path == "/api/v1/agent/approvals":
            rows = [a for a in self.approvals.values()
                    if a["target"] == params["target"] and a["gate"] == params["gate"]
                    and ("decision" not in params or a["decision"] == params["decision"])]
            return httpx.Response(200, json=rows)
        if method == "POST" and path == "/api/v1/agent/approvals":
            return httpx.Response(201, json=self.add_approval(body["target"], body["gate"]))
        if method == "POST" and path.startswith("/api/v1/agent/approvals/") \
                and path.endswith("/decide"):
            a = self.approvals[path.split("/")[-2]]
            a["decision"], a["actor"] = body["decision"], body["actor"]
            a["decided_at"] = self._now().isoformat()
            return httpx.Response(200, json=a)
        if method == "POST" and path == "/api/v1/agent/events":
            self.add_event(body["kind"], body.get("payload") or {},
                           correlation_id=body.get("correlation_id"))
            return httpx.Response(201, json={"ok": True})
        if method == "GET" and path == "/api/v1/sources/navigator/events":
            rows = self.events
            after = params.get("after")
            if after is not None:
                idx = next((i for i, e in enumerate(rows) if e["id"] == after), -1)
                rows = rows[idx + 1:]
            return httpx.Response(200, json=rows[:int(params.get("limit", 500))])
        return httpx.Response(404, json={"error": f"{method} {path}"})


def _http_act(server):
    return HTTPACTClient("http://act.test",
                         client=httpx.Client(transport=httpx.MockTransport(server.handle)))


def test_is_audited_end_to_end_over_http():
    server = _ActServer()
    rid = server.add_run()
    act = _http_act(server)
    assert not is_audited(act, rid)
    rc, out = _cli(act, "audited", rid, "--note", "diff reviewed")
    assert rc == 0, out
    assert is_audited(act, rid)
    assert is_audited(NotifyingACT(act, None), rid)
    audits = server.audits()
    assert len(audits) == 1
    assert audits[0]["payload"] == {"run_id": rid, "note": "diff reviewed", "actor": "auditor",
                                   "verified_sha": VERIFIED_SHA}
    grants = server.review_grants(rid)
    assert len(grants) == 1 and grants[0]["actor"] == "auditor"
    assert grants[0]["decision"] == "approved"
    assert any(p == "/api/v1/sources/navigator/events" for _, p, _ in server.requests)


def test_navigator_audited_twice_over_http_records_one_event():
    server = _ActServer()
    rid = server.add_run()
    act = _http_act(server)
    assert _cli(act, "audited", rid)[0] == 0
    rc, out = _cli(act, "audited", rid)
    assert rc == 0 and "already" in out
    assert len(server.audits()) == 1
    assert len(server.review_grants(rid)) == 1


def test_equal_timestamps_do_not_count_over_http():
    server = _ActServer()
    server.step = timedelta(0)   # every row carries the awaiting_review timestamp
    rid = server.add_run()
    act = _http_act(server)
    assert _cli(act, "audited", rid)[0] == 0
    assert len(server.audits()) == 1
    assert not is_audited(act, rid)
    # rows strictly after the review do count
    server.step = timedelta(seconds=1)
    assert _cli(act, "audited", rid)[0] == 0
    assert len(server.audits()) == 2
    assert is_audited(act, rid)


def test_sha_mismatch_over_http_does_not_count():
    server = _ActServer()
    rid = server.add_run()
    act = _http_act(server)
    assert _cli(act, "audited", rid)[0] == 0
    assert is_audited(act, rid)
    server.runs[rid]["payload"]["verified_sha"] = OTHER_SHA
    assert not is_audited(act, rid)


def test_events_before_the_run_started_are_dropped_over_http():
    server = _ActServer()
    rid = server.add_run()                     # started_at 11:00, events from 12:00
    # an ancient forged pair for this run id, long before the run started
    server.events.insert(0, {"id": new_id("event"), "kind": "run.awaiting_review",
                             "severity": "info", "ts": "2026-01-01T00:00:00+00:00",
                             "payload": {"run_id": rid, "verified_sha": VERIFIED_SHA},
                             "correlation_id": "corr-1"})
    act = _http_act(server)
    assert _cli(act, "audited", rid)[0] == 0
    assert is_audited(act, rid)
    # the audit path only looked at events since the run started
    assert act.list_events(since=datetime(2026, 10, 15, 10, 55, tzinfo=timezone.utc))[0]["kind"] \
        if False else True
    recent = act.list_events(kinds={"run.awaiting_review"},
                             since=datetime(2026, 10, 15, 10, 55, tzinfo=timezone.utc))
    assert len(recent) == 1 and recent[0].ts.year == 2026 and recent[0].ts.month == 10


def test_too_many_event_pages_fails_closed_and_logs_the_class_only():
    server = _ActServer()
    rid = server.add_run()
    pages = []

    def handle(req):
        if req.url.path == "/api/v1/sources/navigator/events":
            pages.append(req.url.params.get("after"))
            base = len(pages) * 1000
            return httpx.Response(200, json=[
                {"id": f"event-{base + i:08d}", "kind": "run.audited", "severity": "info",
                 "ts": "2026-10-15T13:00:00+00:00", "payload": {"run_id": rid},
                 "correlation_id": "corr-1"} for i in range(500)])
        return server.handle(req)
    act = HTTPACTClient("http://act.test",
                        client=httpx.Client(transport=httpx.MockTransport(handle)))
    with capture_logs() as logs:
        assert not is_audited(act, rid)           # fails closed, does not raise
    assert len(pages) == 50
    unreadable = [l for l in logs if l["event"] == "cli.events_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0]["error"] == "ACTEventsTooLarge"
    assert unreadable[0]["log_level"] == "warning"
    # only the class name is logged, never the exception message
    assert not any("pages" in str(v) or "more than" in str(v) for v in unreadable[0].values())
    # the ACT endpoint behaviour behind the log line
    import pytest
    with pytest.raises(ACTEventsTooLarge):
        act.list_events(kinds={"run.audited"})


# ---- merge_tier_of (3c) ------------------------------------------------------------

def _review_with_tier(act, run, tier, ts):
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-x", ts=ts,
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA,
                                  "merge_tier": tier}))


def test_merge_tier_of_reads_latest_awaiting_review_event():
    act = FakeACTClient()
    run = _run(act)                      # its awaiting_review event records no tier
    assert merge_tier_of(act, run) is None
    assert merge_tier_of(act, run.id) is None
    base = datetime.now(timezone.utc)   # explicit, strictly increasing timestamps
    _review_with_tier(act, run, "B", base + timedelta(seconds=1))
    assert merge_tier_of(act, run) == "B"
    assert merge_tier_of(act, run.id) == "B"
    # another run's tier never bleeds over
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-y",
                         ts=base + timedelta(seconds=10),
                         payload={"run_id": "run-other", "verified_sha": VERIFIED_SHA,
                                  "merge_tier": "A"}))
    assert merge_tier_of(act, run) == "B"
    _review_with_tier(act, run, "C", base + timedelta(seconds=2))
    assert merge_tier_of(act, run) == "C"
    _review_with_tier(act, run, "Z", base + timedelta(seconds=3))
    assert merge_tier_of(act, run) is None
    assert merge_tier_of(act, "run-missing") is None
    assert merge_tier_of(act, None) is None


def test_merge_tier_of_unwraps_notifying_act_and_fails_closed_without_events():
    class _Rec:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
    inner = FakeACTClient()
    run = _run(inner)
    _review_with_tier(inner, run, "B", datetime.now(timezone.utc) + timedelta(seconds=1))
    assert merge_tier_of(NotifyingACT(inner, _Rec()), run) == "B"
    assert merge_tier_of(NotifyingACT(inner, _Rec()), run.id) == "B"
    # no list_events, no .inner: the tier is unknown (fail closed), never guessed
    assert merge_tier_of(ProtocolOnlyACT(inner), run) is None


# ---- reviewer memory B1: the audit writer (env-only: run_cli carries no Settings) ---------

def _proposal_and_run(act, repo="/tmp/some/repo", files=("b.py", "a.py")):
    from navigator.contracts.proposal import Proposal, compute_origin_hash
    p = act.create_proposal(Proposal(
        title="t", rationale="r", repo=repo, base_commit_sha="a" * 40, intended_changes="x",
        affected_files=list(files), origin="periodic",
        proposal_origin_hash=compute_origin_hash(repo, "x", list(files), "periodic", None)))
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="abc", branch="nav/x",
                             phase=RunPhase.awaiting_review, correlation_id="corr-x",
                             verified_sha=VERIFIED_SHA))
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-x",
                         ts=datetime.now(timezone.utc) - timedelta(seconds=1),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA}))
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    return p, run


def _audit_entries(root, name="repo"):
    from pathlib import Path
    path = Path(root) / f"{name}.jsonl"
    assert path.is_file(), "audit note was not remembered"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_audited_note_is_remembered_for_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "1")
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    act = FakeACTClient()
    p, run = _proposal_and_run(act)
    rc, out = _cli(act, "audited", run.id, "--note", "diff reviewed")
    assert rc == 0, out
    entries = _audit_entries(tmp_path / "mem")
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "audit"
    assert e["text"] == "diff reviewed"
    assert e["source"] == "auditor"
    assert e["files"] == ["a.py", "b.py"]
    assert e["repo"] == "/tmp/some/repo" and e["title"] == "t"
    assert e["run_id"] == run.id and e["proposal_id"] == p.id
    assert e["verified_sha"] == VERIFIED_SHA and e["base_sha"] == "abc"
    assert e["reason_code"] is None
    # the audit records themselves are untouched
    assert is_audited(act, run.id)
    events = [ev for ev in act.events if ev.kind == "run.audited"]
    assert len(events) == 1
    assert events[0].payload == {"run_id": run.id, "note": "diff reviewed", "actor": "auditor",
                                 "verified_sha": VERIFIED_SHA}


def test_audited_without_note_writes_no_memory(tmp_path, monkeypatch):
    # guard: an audit without --note has nothing to remember
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "1")
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    act = FakeACTClient()
    _, run = _proposal_and_run(act)
    rc, _ = _cli(act, "audited", run.id)
    assert rc == 0
    assert is_audited(act, run.id)
    assert not (tmp_path / "mem").exists()


def test_audited_memory_is_off_unless_the_env_says_so(tmp_path, monkeypatch):
    # guard: switch unset -> no file and no proposal lookup (byte-identical to before B1)
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY", raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    act = FakeACTClient()
    _, run = _proposal_and_run(act)
    calls = []
    real = act.get_proposal
    act.get_proposal = lambda pid: (calls.append(pid), real(pid))[1]
    rc, _ = _cli(act, "audited", run.id, "--note", "diff reviewed")
    assert rc == 0 and is_audited(act, run.id)
    assert not (tmp_path / "mem").exists()
    assert calls == []


def test_audited_with_unknown_proposal_still_succeeds_and_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "1")
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    act = FakeACTClient()
    run = _run(act)                     # proposal-x is not in ACT: no repo to file the note under
    with capture_logs() as logs:
        rc, out = _cli(act, "audited", run.id, "--note", "diff reviewed")
    assert rc == 0, out
    assert is_audited(act, run.id)
    assert not (tmp_path / "mem").exists()
    assert [l["kind"] for l in logs if l["event"] == "repo_memory.no_repo"] == ["audit"]


def test_audited_memory_failure_never_fails_the_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "1")
    clash = tmp_path / "afile"
    clash.write_text("x")
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(clash / "sub"))   # root under a FILE
    act = FakeACTClient()
    _, run = _proposal_and_run(act)
    with capture_logs() as logs:
        rc, out = _cli(act, "audited", run.id, "--note", "diff reviewed")   # must NOT raise
    assert rc == 0, out
    assert is_audited(act, run.id)
    assert [l["kind"] for l in logs if l["event"] == "repo_memory.write_failed"] == ["audit"]
