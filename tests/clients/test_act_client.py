import pytest
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import ProposalStatus, RiskTier, ApprovalGate, ApprovalDecision

def _proposal():
    return Proposal(title="t", rationale="r", repo="trader", base_commit_sha="abc",
                    intended_changes="x", affected_files=["a.py"], risk_tier=RiskTier.low,
                    origin="periodic",
                    proposal_origin_hash=compute_origin_hash("trader","x",["a.py"],"periodic",None))

def test_fake_act_list_returns_copies_not_live_refs():
    # mutating a returned object must NOT corrupt the in-memory store
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    got = act.list_proposals(status=ProposalStatus.proposed)[0]
    got.status = ProposalStatus.approved  # mutate the returned object
    # store is unchanged: still proposed, not approved
    assert act.list_proposals(status=ProposalStatus.proposed)[0].id == p.id
    assert act.list_proposals(status=ProposalStatus.approved) == []

def test_fake_act_proposal_and_approval_flow():
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    assert act.list_proposals(status=ProposalStatus.proposed)[0].id == p.id
    act.set_proposal_status(p.id, ProposalStatus.approved)
    assert act.list_proposals(status=ProposalStatus.approved)[0].id == p.id
    a = act.create_approval(target=p.id, gate=ApprovalGate.merge)
    act.decide_approval(a.id, ApprovalDecision.approved, actor="manoj")
    assert act.list_approvals(target=p.id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.approved)

# (test_http_act_is_inert_until_patch removed 2026-07-03: the ACT agent-resource
# patch has landed; HTTPACTClient is now the real client — see tests below)

# ---- protocol additions for HTTP wiring ------------------------------------

def test_fake_act_get_proposal_returns_copy_or_none():
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    got = act.get_proposal(p.id)
    assert got is not None and got.id == p.id
    got.status = ProposalStatus.approved  # mutating the copy must not touch the store
    assert act.get_proposal(p.id).status == ProposalStatus.proposed
    assert act.get_proposal("proposal-missing") is None

def test_fake_act_list_runs_for_proposal_includes_completed():
    from navigator.contracts.run import Run
    from navigator.contracts.enums import RunStatus
    act = FakeACTClient()
    p = _proposal(); act.create_proposal(p)
    r1 = Run(proposal_id=p.id, base_commit_sha="abc", branch="nav/r1")
    r2 = Run(proposal_id="proposal-other", base_commit_sha="abc", branch="nav/r2")
    act.create_run(r1); act.create_run(r2)
    r1.status = RunStatus.completed; act.update_run(r1)
    got = act.list_runs_for_proposal(p.id)
    assert [r.id for r in got] == [r1.id]           # completed run still visible
    assert got[0].status == RunStatus.completed
    got[0].status = RunStatus.executing              # copy, not live ref
    assert act.list_runs_for_proposal(p.id)[0].status == RunStatus.completed

# ---- HTTPACTClient (httpx.MockTransport emulating the ACT agent resource) ---

import json
import httpx
from datetime import datetime, timezone
from navigator.contracts.run import Run, Checkpoint
from navigator.contracts.event import Event
from navigator.contracts.enums import RunStatus, RunPhase, Severity


class _ActFake:
    """Tiny in-handler emulation of ACT's /api/v1/agent surface, recording requests."""
    def __init__(self):
        self.proposals: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.events: list[dict] = []
        self.requests: list[tuple[str, str, dict | None]] = []
        self._n = 0

    def _ulid(self, prefix):
        self._n += 1
        return f"{prefix}-SRV{self._n:04d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, str(request.url), body))
        now = datetime.now(timezone.utc).isoformat()
        if method == "POST" and path == "/api/v1/agent/proposals":
            pid = body.get("id") or self._ulid("proposal")
            if pid in self.proposals:
                return httpx.Response(409, json={"detail": "proposal_id_exists"})
            row = {"id": pid, "schema_version": body["schema_version"], "status": "proposed",
                   "origin": body["origin"], "proposal_origin_hash": body["proposal_origin_hash"],
                   "repo": body["repo"], "base_commit_sha": body["base_commit_sha"],
                   "risk_tier": body["risk_tier"], "priority": body["priority"],
                   "created_at": now, "payload": body.get("payload") or {}}
            self.proposals[pid] = row
            return httpx.Response(200, json=row)
        if method == "GET" and path.startswith("/api/v1/agent/proposals/"):
            pid = path.rsplit("/", 1)[1]
            row = self.proposals.get(pid)
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"detail": "proposal_not_found"})
        if method == "GET" and path == "/api/v1/agent/proposals":
            status = request.url.params.get("status")
            rows = [r for r in self.proposals.values() if status is None or r["status"] == status]
            return httpx.Response(200, json=rows)
        if method == "POST" and path.endswith("/status"):
            pid = path.split("/")[-2]
            row = self.proposals.get(pid)
            if not row:
                return httpx.Response(404, json={"detail": "proposal_not_found"})
            row["status"] = body["status"]
            return httpx.Response(200, json=row)
        if method == "POST" and path == "/api/v1/agent/runs":
            rid = body.get("id") or self._ulid("run")
            if rid in self.runs:
                return httpx.Response(409, json={"detail": "run_id_exists"})
            row = {"id": rid, "schema_version": body["schema_version"], "proposal_id": body["proposal_id"],
                   "status": "executing", "phase": body["phase"], "base_commit_sha": body["base_commit_sha"],
                   "branch": body["branch"], "worktree_path": body.get("worktree_path"),
                   "correlation_id": body.get("correlation_id"), "started_at": now, "ended_at": None,
                   "cost_usd": body.get("cost_usd", 0.0), "payload": body.get("payload") or {}}
            self.runs[rid] = row
            return httpx.Response(200, json=row)
        if method == "GET" and path == "/api/v1/agent/runs":
            open_only = request.url.params.get("open_only", "true") == "true"
            proposal_id = request.url.params.get("proposal_id")
            rows = [r for r in self.runs.values()
                    if (not open_only or r["status"] == "executing")
                    and (proposal_id is None or r["proposal_id"] == proposal_id)]
            return httpx.Response(200, json=rows)
        if method == "POST" and path.startswith("/api/v1/agent/runs/"):
            rid = path.rsplit("/", 1)[1]
            row = self.runs.get(rid)
            if not row:
                return httpx.Response(404, json={"detail": "run_not_found"})
            for k in ("status", "phase", "worktree_path", "ended_at", "cost_usd", "payload"):
                if body.get(k) is not None:
                    row[k] = body[k]
            return httpx.Response(200, json=row)
        if method == "POST" and path == "/api/v1/agent/approvals":
            aid = self._ulid("approval")
            row = {"id": aid, "schema_version": 1, "target": body["target"], "gate": body["gate"],
                   "decision": "pending", "actor": None, "decided_at": None, "note": None,
                   "correlation_id": body.get("correlation_id")}
            self.approvals[aid] = row
            return httpx.Response(200, json=row)
        if method == "POST" and path.endswith("/decide"):
            aid = path.split("/")[-2]
            row = self.approvals.get(aid)
            if not row:
                return httpx.Response(404, json={"detail": "approval_not_found"})
            row["decision"] = body["decision"]; row["actor"] = body.get("actor"); row["decided_at"] = now
            return httpx.Response(200, json=row)
        if method == "GET" and path == "/api/v1/agent/approvals":
            p = request.url.params
            rows = [r for r in self.approvals.values()
                    if (p.get("target") is None or r["target"] == p.get("target"))
                    and (p.get("gate") is None or r["gate"] == p.get("gate"))
                    and (p.get("decision") is None or r["decision"] == p.get("decision"))]
            return httpx.Response(200, json=rows)
        if method == "POST" and path == "/api/v1/agent/events":
            self.events.append(body)
            return httpx.Response(200, json={"id": self._ulid("ev"), "source": "navigator",
                                             "kind": body["kind"], "severity": body.get("severity", "info"),
                                             "ts": now, "payload": body.get("payload") or {},
                                             "correlation_id": body.get("correlation_id"), "agent_id": None})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})


def _http_act():
    fake = _ActFake()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return HTTPACTClient(base_url="http://act.test", client=client), fake


def test_http_create_proposal_roundtrips_navigator_fields_via_payload():
    act, fake = _http_act()
    p = _proposal()
    stored = act.create_proposal(p)
    assert stored.id == p.id                      # client id is the identity
    assert fake.proposals[p.id]["risk_tier"] == "low"
    # Navigator-only fields ride in payload...
    assert fake.proposals[p.id]["payload"]["title"] == "t"
    # ...and come back intact through list_proposals
    got = act.list_proposals(status=ProposalStatus.proposed)
    assert len(got) == 1
    q = got[0]
    assert (q.id, q.title, q.rationale, q.intended_changes, q.affected_files) == \
           (p.id, p.title, p.rationale, p.intended_changes, p.affected_files)
    assert q.status == ProposalStatus.proposed and q.proposal_origin_hash == p.proposal_origin_hash

def test_http_get_proposal_returns_none_on_404():
    act, fake = _http_act()
    assert act.get_proposal("proposal-missing") is None
    p = _proposal(); act.create_proposal(p)
    assert act.get_proposal(p.id).id == p.id

def test_http_set_proposal_status_posts_and_errors_surface():
    act, fake = _http_act()
    p = _proposal(); act.create_proposal(p)
    act.set_proposal_status(p.id, ProposalStatus.approved)
    assert fake.proposals[p.id]["status"] == "approved"
    with pytest.raises(httpx.HTTPStatusError):
        act.set_proposal_status("proposal-missing", ProposalStatus.approved)

def test_http_create_run_preserves_client_id_and_maps_extras_to_payload():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1",
            checkpoints=[Checkpoint(sha="c1", label="cp")], completed_tasks=["t1"])
    stored = act.create_run(r)
    assert stored.id == r.id and stored.branch == "nav/run-1"
    sent = fake.runs[r.id]
    assert sent["payload"]["completed_tasks"] == ["t1"]
    assert sent["payload"]["checkpoints"] == [{"sha": "c1", "label": "cp"}]

def test_http_update_run_and_open_run_roundtrip():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    r.phase = RunPhase.awaiting_review
    r.checkpoints = [Checkpoint(sha="c2", label="cp")]
    act.update_run(r)
    open_runs = act.list_open_runs()
    assert len(open_runs) == 1
    got = open_runs[0]
    assert got.id == r.id and got.phase == RunPhase.awaiting_review
    assert got.checkpoints == [Checkpoint(sha="c2", label="cp")]
    # completing the run drops it from the open list
    r.status = RunStatus.completed
    r.ended_at = datetime.now(timezone.utc)
    act.update_run(r)
    assert act.list_open_runs() == []

def test_http_list_runs_for_proposal_includes_completed():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    r.status = RunStatus.failed; act.update_run(r)
    got = act.list_runs_for_proposal("proposal-x")
    assert [x.id for x in got] == [r.id] and got[0].status == RunStatus.failed
    assert act.list_runs_for_proposal("proposal-other") == []

def test_http_approval_create_decide_list():
    act, fake = _http_act()
    a = act.create_approval(target="run-1", gate=ApprovalGate.merge)
    assert a.gate == ApprovalGate.merge and a.decision == ApprovalDecision.pending
    act.decide_approval(a.id, ApprovalDecision.approved, actor="manoj")
    got = act.list_approvals(target="run-1", gate=ApprovalGate.merge,
                             decision=ApprovalDecision.approved)
    assert [x.id for x in got] == [a.id] and got[0].actor == "manoj"
    # decision filter actually filters
    assert act.list_approvals(target="run-1", gate=ApprovalGate.merge,
                              decision=ApprovalDecision.declined) == []

def test_http_emit_event_maps_warning_to_act_warn():
    act, fake = _http_act()
    act.emit_event(Event(kind="run.awaiting_review", severity=Severity.warning,
                         correlation_id="corr-1", payload={"diff": "d"}))
    assert fake.events == [{"kind": "run.awaiting_review", "severity": "warn",
                            "correlation_id": "corr-1", "payload": {"diff": "d"}}]

def test_default_client_timeout_is_generous():
    from navigator.clients.act_client import HTTPACTClient
    c = HTTPACTClient("http://x")
    assert c.client.timeout.read >= 30


# ---- N12f: base_commit_sha survives update_run (a rebase moves the base) ---------

def test_http_update_run_persists_rebased_base_commit():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    assert fake.runs[r.id]["base_commit_sha"] == "abc"
    r.base_commit_sha = "def"
    act.update_run(r)
    assert fake.runs[r.id]["payload"]["base_commit_sha"] == "def"
    assert fake.runs[r.id]["base_commit_sha"] == "abc"  # the column is fixed at creation
    got = act.list_open_runs()
    assert [x.id for x in got] == [r.id] and got[0].base_commit_sha == "def"
    assert act.list_runs_for_proposal("proposal-x")[0].base_commit_sha == "def"


def test_http_run_without_base_in_payload_uses_the_column():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    fake.runs[r.id]["payload"].pop("base_commit_sha", None)  # row written by older navigator
    assert act.list_open_runs()[0].base_commit_sha == "abc"


def test_fake_update_run_only_keeps_http_fields():
    act = FakeACTClient()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    changed = r.model_copy(deep=True)
    changed.branch = "x"                      # not sent by HTTPACTClient.update_run
    changed.phase = RunPhase.awaiting_review  # sent
    changed.base_commit_sha = "def"           # sent (payload)
    act.update_run(changed)
    stored = act.runs[r.id]
    assert stored.branch == "nav/run-1"
    assert stored.phase == RunPhase.awaiting_review and stored.base_commit_sha == "def"
    assert act.list_open_runs()[0].base_commit_sha == "def"


def test_fake_update_run_does_not_mutate_earlier_snapshots():
    act = FakeACTClient()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1",
            verified_sha="old")
    act.create_run(r)
    snapshot = act.runs[r.id]
    changed = r.model_copy(deep=True)
    changed.verified_sha = "new"
    act.update_run(changed)
    assert snapshot.verified_sha == "old"          # replace-on-write, never in place
    assert act.runs[r.id].verified_sha == "new"
    assert act.runs[r.id] is not snapshot and act.runs[r.id] is not changed
    changed.verified_sha = "later"                 # the argument is not aliased either
    assert act.runs[r.id].verified_sha == "new"
