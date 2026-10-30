import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
import httpx
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.contracts.approval import Approval
from navigator.contracts.event import Event
from navigator.contracts.enums import ProposalStatus, ApprovalGate, ApprovalDecision

class ACTClient(Protocol):
    def create_proposal(self, p: Proposal) -> Proposal: ...
    def get_proposal(self, proposal_id: str) -> Proposal | None: ...
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]: ...
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None: ...
    def create_run(self, r: Run) -> Run: ...
    def update_run(self, r: Run) -> None: ...
    def list_open_runs(self) -> list[Run]: ...
    def list_all_runs(self) -> list[Run]: ...
    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]: ...
    def create_approval(self, target: str, gate: ApprovalGate) -> Approval: ...
    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str,
                        note: str | None = None) -> None: ...
    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]: ...
    def emit_event(self, event: Event) -> None: ...
    def list_events(self, kinds: set[str] | None = None,
                    since: datetime | None = None) -> list: ...
    def get_doc(self, key: str) -> tuple[dict, int] | None: ...
    def put_doc(self, key: str, body: dict, actor: str,
                expected_version: int | None = None) -> int: ...

# The run fields HTTPACTClient.update_run persists (columns status/phase/worktree_path/
# ended_at/cost_usd plus the _run_payload fields). FakeACTClient.update_run keeps
# exactly these so the fake can never hide a field the HTTP client drops.
_RUN_UPDATE_FIELDS = ("status", "phase", "worktree_path", "ended_at", "cost_usd",
                      "completed_tasks", "target_branch", "verification", "model_calls",
                      "verified_sha", "checkpoints", "heartbeat_at", "base_commit_sha")


class FakeACTClient:
    def __init__(self):
        self.proposals: dict[str, Proposal] = {}
        self.runs: dict[str, Run] = {}
        self.approvals: dict[str, Approval] = {}
        self.events: list[Event] = []
        # key -> (body, version, actor); versions start at 1
        self.docs: dict[str, tuple[dict, int, str]] = {}
    def create_proposal(self, p: Proposal) -> Proposal:
        self.proposals[p.id] = p; return p
    def get_proposal(self, proposal_id: str) -> Proposal | None:
        p = self.proposals.get(proposal_id)
        return p.model_copy(deep=True) if p is not None else None
    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        return [p.model_copy(deep=True) for p in self.proposals.values()
                if status is None or p.status == status]
    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.proposals[proposal_id].status = status
    def create_run(self, r: Run) -> Run:
        self.runs[r.id] = r; return r
    def update_run(self, r: Run) -> None:
        # replace-on-write of exactly the fields the HTTP client sends: the stored
        # object is never mutated in place (callers may hold earlier references)
        stored = self.runs.get(r.id)
        fresh = r.model_copy(deep=True)
        if stored is None:
            self.runs[r.id] = fresh
            return
        self.runs[r.id] = stored.model_copy(
            update={n: getattr(fresh, n) for n in _RUN_UPDATE_FIELDS}, deep=True)
    def list_open_runs(self) -> list[Run]:
        from navigator.contracts.enums import RunStatus
        return [r.model_copy(deep=True) for r in self.runs.values() if r.status == RunStatus.executing]
    def list_all_runs(self) -> list[Run]:
        return [r.model_copy(deep=True) for r in self.runs.values()]
    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]:
        return [r.model_copy(deep=True) for r in self.runs.values() if r.proposal_id == proposal_id]
    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        a = Approval(target=target, gate=gate); self.approvals[a.id] = a; return a
    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str,
                        note: str | None = None) -> None:
        a = self.approvals[approval_id]
        a.decision = decision; a.actor = actor; a.decided_at = datetime.now(timezone.utc)
        if note is not None:
            a.note = note
    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        return [a.model_copy(deep=True) for a in self.approvals.values()
                if a.target == target and a.gate == gate
                and (decision is None or a.decision == decision)]
    def emit_event(self, event: Event) -> None:
        self.events.append(event)
    def list_events(self, kinds: set[str] | None = None,
                    since: datetime | None = None) -> list[Event]:
        floor = _parse_ts(since)
        return [e.model_copy(deep=True) for e in self.events
                if (kinds is None or e.kind in kinds) and _not_before(e.ts, floor)]
    def get_doc(self, key: str) -> tuple[dict, int] | None:
        entry = self.docs.get(key)
        if entry is None:
            return None
        body, version, _actor = entry
        return (copy.deepcopy(body), version)
    def put_doc(self, key: str, body: dict, actor: str,
                expected_version: int | None = None) -> int:
        existing = self.docs.get(key)
        current = existing[1] if existing is not None else 0
        if expected_version is not None and expected_version != current:
            raise DocVersionConflict(current)
        self.docs[key] = (copy.deepcopy(body), current + 1, actor)
        return current + 1

# ACT clamps list endpoints at 500; request the max so nothing is silently dropped.
_LIST_LIMIT = 500
# events are paged with after=<cursor>; a page shorter than the limit is the last one
_EVENT_PAGE_LIMIT = 500
_EVENT_MAX_PAGES = 50


class ACTEventsTooLarge(RuntimeError):
    """list_events gave up: ACT holds more than _EVENT_MAX_PAGES full pages of
    navigator events (callers that only need recent ones should pass `since`)."""


class DocVersionConflict(RuntimeError):
    """put_doc was given an expected_version that is not the document's current
    version (ACT answered 409); `.current_version` is the version ACT holds now."""

    def __init__(self, current_version: int):
        super().__init__(f"document version conflict (current {current_version})")
        self.current_version = current_version


def _not_before(ts, floor: datetime | None) -> bool:
    """True unless `ts` parses to a timestamp strictly before `floor`. An unparsable
    or missing ts is kept (never silently dropped); equal timestamps are kept."""
    if floor is None:
        return True
    t = _parse_ts(ts)
    return t is None or t >= floor


def _parse_ts(value) -> datetime | None:
    """ISO timestamp from ACT as an aware datetime (naive = UTC); None if unusable."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class ACTEvent:
    """An event read back from ACT (its severity vocabulary differs from Event's, so
    this is deliberately not the Event model)."""
    kind: str
    ts: datetime | None
    payload: dict = field(default_factory=dict)
    correlation_id: str | None = None
    id: str | None = None

# ACT's severity vocabulary uses "warn"; Navigator's enum uses "warning".
_SEVERITY_TO_ACT = {"warning": "warn"}


class HTTPACTClient:
    """REST client for ACT's /api/v1/agent resource (agent-resource patch, applied
    2026-07-02, plus the HTTP-client support routes). Navigator-only fields that
    have no ACT column (title, rationale, intended_changes, affected_files,
    checkpoints, ...) round-trip through the JSON payload column. Navigator-minted
    ids are sent on create and are the identity (ACT 409s on conflict)."""

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = f"{base_url.rstrip('/')}/api/v1/agent"
        # local ACT can stall past httpx's 5s default (live ReadTimeout 2026-07-15)
        self.client = client or httpx.Client(timeout=30.0)

    # ---- proposals ----------------------------------------------------------

    def create_proposal(self, p: Proposal) -> Proposal:
        r = self.client.post(f"{self.base_url}/proposals", json={
            "id": p.id, "schema_version": p.schema_version, "origin": p.origin,
            "proposal_origin_hash": p.proposal_origin_hash, "repo": p.repo,
            "base_commit_sha": p.base_commit_sha, "risk_tier": p.risk_tier.value,
            "priority": p.priority, "payload": {
                "title": p.title, "rationale": p.rationale,
                "intended_changes": p.intended_changes,
                "affected_files": p.affected_files,
                "supersedes_proposal_id": p.supersedes_proposal_id,
            }})
        r.raise_for_status()
        return self._proposal_from(r.json())

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        r = self.client.get(f"{self.base_url}/proposals/{proposal_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._proposal_from(r.json())

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        params: dict = {"limit": _LIST_LIMIT}
        if status is not None:
            params["status"] = status.value
        r = self.client.get(f"{self.base_url}/proposals", params=params)
        r.raise_for_status()
        return [self._proposal_from(d) for d in r.json()]

    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.client.post(f"{self.base_url}/proposals/{proposal_id}/status",
                         json={"status": status.value}).raise_for_status()

    @staticmethod
    def _proposal_from(d: dict) -> Proposal:
        pl = d.get("payload") or {}
        return Proposal(
            id=d["id"], schema_version=d["schema_version"], priority=d["priority"],
            origin=d["origin"], proposal_origin_hash=d["proposal_origin_hash"],
            supersedes_proposal_id=pl.get("supersedes_proposal_id"),
            title=pl.get("title", ""), rationale=pl.get("rationale", ""),
            repo=d["repo"], base_commit_sha=d["base_commit_sha"],
            intended_changes=pl.get("intended_changes", ""),
            affected_files=pl.get("affected_files") or [],
            risk_tier=d["risk_tier"], created_at=d["created_at"], status=d["status"])

    # ---- runs ----------------------------------------------------------------

    def create_run(self, r: Run) -> Run:
        resp = self.client.post(f"{self.base_url}/runs", json={
            "id": r.id, "schema_version": r.schema_version, "proposal_id": r.proposal_id,
            "phase": r.phase.value, "base_commit_sha": r.base_commit_sha,
            "branch": r.branch, "worktree_path": r.worktree_path,
            "correlation_id": r.correlation_id, "cost_usd": r.cost_usd,
            "payload": self._run_payload(r)})
        resp.raise_for_status()
        return self._run_from(resp.json())

    def update_run(self, r: Run) -> None:
        self.client.post(f"{self.base_url}/runs/{r.id}", json={
            "status": r.status.value, "phase": r.phase.value,
            "worktree_path": r.worktree_path,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "cost_usd": r.cost_usd,
            "payload": self._run_payload(r)}).raise_for_status()

    def list_open_runs(self) -> list[Run]:
        r = self.client.get(f"{self.base_url}/runs", params={"limit": _LIST_LIMIT})
        r.raise_for_status()
        return [self._run_from(d) for d in r.json()]

    def list_all_runs(self) -> list[Run]:
        r = self.client.get(f"{self.base_url}/runs", params={
            "open_only": "false", "limit": _LIST_LIMIT})
        r.raise_for_status()
        return [self._run_from(d) for d in r.json()]

    def list_runs_for_proposal(self, proposal_id: str) -> list[Run]:
        r = self.client.get(f"{self.base_url}/runs", params={
            "proposal_id": proposal_id, "open_only": "false", "limit": _LIST_LIMIT})
        r.raise_for_status()
        return [self._run_from(d) for d in r.json()]

    @staticmethod
    def _run_payload(r: Run) -> dict:
        return {"completed_tasks": r.completed_tasks, "target_branch": r.target_branch,
                "verification": r.verification, "model_calls": r.model_calls,
                "verified_sha": r.verified_sha,
                "checkpoints": [c.model_dump() for c in r.checkpoints],
                "heartbeat_at": r.heartbeat_at.isoformat(),
                # the ACT column is fixed at creation; a rebase moves the base
                "base_commit_sha": r.base_commit_sha}

    @staticmethod
    def _run_from(d: dict) -> Run:
        pl = d.get("payload") or {}
        return Run(
            id=d["id"], schema_version=d["schema_version"], proposal_id=d["proposal_id"],
            status=d["status"], phase=d["phase"],
            heartbeat_at=pl.get("heartbeat_at") or d["started_at"],
            worktree_path=d.get("worktree_path"),
            base_commit_sha=pl.get("base_commit_sha") or d["base_commit_sha"],
            branch=d["branch"], target_branch=pl.get("target_branch"),
            verification=pl.get("verification") or [],
            verified_sha=pl.get("verified_sha") or None,
            model_calls=pl.get("model_calls") or 0,
            completed_tasks=pl.get("completed_tasks") or [],
            checkpoints=pl.get("checkpoints") or [], cost_usd=d.get("cost_usd", 0.0),
            correlation_id=d.get("correlation_id"), started_at=d["started_at"],
            ended_at=d.get("ended_at"))

    # ---- approvals -----------------------------------------------------------

    def create_approval(self, target: str, gate: ApprovalGate) -> Approval:
        r = self.client.post(f"{self.base_url}/approvals",
                             json={"target": target, "gate": gate.value})
        r.raise_for_status()
        return self._approval_from(r.json())

    def decide_approval(self, approval_id: str, decision: ApprovalDecision, actor: str,
                        note: str | None = None) -> None:
        body: dict = {"decision": decision.value, "actor": actor}
        if note is not None:
            body["note"] = note  # ACT stores it (ApprovalDecideRequest.note)
        self.client.post(f"{self.base_url}/approvals/{approval_id}/decide",
                         json=body).raise_for_status()

    def list_approvals(self, target: str, gate: ApprovalGate,
                       decision: ApprovalDecision | None = None) -> list[Approval]:
        params: dict = {"target": target, "gate": gate.value, "limit": _LIST_LIMIT}
        if decision is not None:
            params["decision"] = decision.value
        r = self.client.get(f"{self.base_url}/approvals", params=params)
        r.raise_for_status()
        return [self._approval_from(d) for d in r.json()]

    @staticmethod
    def _approval_from(d: dict) -> Approval:
        return Approval(
            id=d["id"], schema_version=d["schema_version"], target=d["target"],
            gate=d["gate"], decision=d["decision"], actor=d.get("actor"),
            decided_at=d.get("decided_at"), note=d.get("note"),
            correlation_id=d.get("correlation_id"))

    # ---- events ---------------------------------------------------------------

    def emit_event(self, event: Event) -> None:
        self.client.post(f"{self.base_url}/events", json={
            "kind": event.kind,
            "severity": _SEVERITY_TO_ACT.get(event.severity.value, event.severity.value),
            "correlation_id": event.correlation_id,
            "payload": event.payload}).raise_for_status()

    @property
    def _root_url(self) -> str:
        """<act>/api/v1: the agent resource's base_url without its trailing '/agent'."""
        suffix = "/agent"
        return self.base_url[:-len(suffix)] if self.base_url.endswith(suffix) else self.base_url

    @staticmethod
    def _event_from(d: dict) -> ACTEvent:
        payload = d.get("payload")
        return ACTEvent(kind=str(d.get("kind", "")), ts=_parse_ts(d.get("ts")),
                        payload=payload if isinstance(payload, dict) else {},
                        correlation_id=d.get("correlation_id"), id=d.get("id"))

    def list_events(self, kinds: set[str] | None = None,
                    since: datetime | None = None) -> list[ACTEvent]:
        """Every event ACT holds for source 'navigator' (only those of `kinds` when
        given, and when `since` is given only those whose ts is not before it; a
        naive `since` means UTC, an event with an unparsable ts is kept), oldest
        first. Pages through GET /api/v1/sources/navigator/events with limit=500 and
        after=<cursor> until a page is shorter than the limit; the cursor is the
        response's next_cursor or, for a bare list, the last event id. The ACT API
        has no newest-first order, so `since` is applied client-side and does not
        shorten the walk. More than 50 pages is an ACTEventsTooLarge (a RuntimeError)
        rather than an unbounded walk."""
        url = f"{self._root_url}/sources/navigator/events"
        floor = _parse_ts(since)
        out: list[ACTEvent] = []
        after = None
        for _ in range(_EVENT_MAX_PAGES):
            params: dict = {"limit": _EVENT_PAGE_LIMIT}
            if after is not None:
                params["after"] = after
            r = self.client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                page = data.get("events")
                if page is None:
                    page = data.get("items") or []
                cursor = data.get("next_cursor")
            else:
                page, cursor = data, None
            if not isinstance(page, list):
                raise RuntimeError("ACT events: response is not a list of events")
            for d in page:
                if isinstance(d, dict) and (kinds is None or d.get("kind") in kinds):
                    ev = self._event_from(d)
                    if _not_before(ev.ts, floor):
                        out.append(ev)
            if len(page) < _EVENT_PAGE_LIMIT:
                return out
            if cursor is None and isinstance(page[-1], dict):
                cursor = page[-1].get("id")
            if cursor is None:
                raise RuntimeError("ACT events: full page without a cursor")
            after = cursor
        raise ACTEventsTooLarge(f"ACT events: more than {_EVENT_MAX_PAGES} pages")

    # ---- documents ------------------------------------------------------------

    def get_doc(self, key: str) -> tuple[dict, int] | None:
        """(body, version) of ACT document `key` via GET /api/v1/agent/docs/<key>,
        or None when ACT answers 404. Keys are fixed constants like
        "navigator/settings"; the key is passed through as-is (ACT's {key:path}
        route accepts the "/"), so a key containing "?", "#" or ".." is not
        supported. A plain 404 from an ACT that predates the docs route also reads
        as None on purpose: the daemon then runs on its environment settings, which
        is today's behaviour (design D2, byte-identical default). Any other error
        status raises httpx.HTTPStatusError."""
        r = self.client.get(f"{self.base_url}/docs/{key}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        return (d["body"], int(d["version"]))

    def put_doc(self, key: str, body: dict, actor: str,
                expected_version: int | None = None) -> int:
        """Write ACT document `key` via PUT /api/v1/agent/docs/<key> and return the
        new version. A 409 (stale expected_version) raises DocVersionConflict
        carrying ACT's current_version; any other error status raises
        httpx.HTTPStatusError."""
        r = self.client.put(f"{self.base_url}/docs/{key}", json={
            "body": body, "actor": actor, "expected_version": expected_version})
        if r.status_code == 409:
            raise DocVersionConflict(int(r.json().get("current_version", 0)))
        r.raise_for_status()
        return int(r.json()["version"])
