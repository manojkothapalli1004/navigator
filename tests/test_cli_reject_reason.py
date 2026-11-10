"""`navigator reject <run> --reason CODE`: the decline reason is stored as the ACT
approval note ('reason=CODE') and lands in the declined outcome's reason_code."""
import io
import json
from types import SimpleNamespace

from navigator.cli import REASON_CODES, run_cli
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.run import Run
from navigator.runtime.executor import _decline_reason, decline_run
from navigator.runtime.outcome_store import DECLINED_LESSON, OutcomeStore


def _setup():
    act = FakeACTClient()
    p = act.create_proposal(Proposal(
        title="t", rationale="r", repo="/r", base_commit_sha="a" * 40, intended_changes="x",
        affected_files=["a.py"], origin="periodic",
        proposal_origin_hash=compute_origin_hash("/r", "x", ["a.py"], "periodic", None)))
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="a" * 40, branch="nav/x"))
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    return act, run


def _cli(act, *argv):
    out = io.StringIO()
    rc = run_cli(list(argv), act=act, out=out)
    return rc, out.getvalue()


def _settings(tmp_path):
    return SimpleNamespace(outcome_path=str(tmp_path / "o.jsonl"))


def _declined(act, run):
    return act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.declined)


def _pending(act, run):
    return act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.pending)


def test_reject_with_reason_stores_note():
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id, "--reason", "unsafe")
    assert rc == 0, out
    assert _pending(act, run) == []
    declined = _declined(act, run)
    assert len(declined) == 1
    assert declined[0].note == "reason=unsafe"
    assert declined[0].actor == "operator"


def test_reject_with_reason_and_text_stores_note():
    """B2: words after the code are the free-text reason: 'reason=CODE: text'."""
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id, "--reason", "unsafe", "touches", "auth")
    assert rc == 0, out
    assert _pending(act, run) == []
    declined = _declined(act, run)
    assert len(declined) == 1
    assert declined[0].note == "reason=unsafe: touches auth"
    assert declined[0].actor == "operator"
    # an unknown code is still refused, text or not
    act2, run2 = _setup()
    rc, out = _cli(act2, "reject", run2.id, "--reason", "bogus", "touches", "auth")
    assert rc == 2 and "bogus" in out
    assert len(_pending(act2, run2)) == 1 and _declined(act2, run2) == []


def test_reject_with_reason_code_trailing_colon_accepted():
    """Rev2 item 4: '--reason unsafe:' (the natural 'CODE: text' spelling) is the code
    'unsafe'; the note never carries the colon twice."""
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id, "--reason", "unsafe:")
    assert rc == 0, out
    assert _declined(act, run)[0].note == "reason=unsafe"
    act2, run2 = _setup()
    rc, out = _cli(act2, "reject", run2.id, "--reason", "unsafe:", "touches", "auth")
    assert rc == 0, out
    assert _declined(act2, run2)[0].note == "reason=unsafe: touches auth"


def test_reason_note_collapses_whitespace_and_omits_empty_text():
    from navigator.cli import reason_note  # new in B2
    assert reason_note("unsafe") == "reason=unsafe"
    assert reason_note("unsafe", "") == "reason=unsafe"
    assert reason_note("other", "   \t ") == "reason=other"
    assert reason_note("unsafe", "  touches   auth \n") == "reason=unsafe: touches auth"
    assert reason_note("low_value", "too small") == "reason=low_value: too small"


def test_reject_with_unknown_reason_is_refused_and_lists_codes():
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id, "--reason", "bogus")
    assert rc == 2
    assert "bogus" in out and "unsafe" in out
    for code in REASON_CODES:
        assert code in out
    assert len(_pending(act, run)) == 1
    assert _declined(act, run) == []


def test_reject_without_reason_leaves_note_none():
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id)
    assert rc == 0, out
    declined = _declined(act, run)
    assert len(declined) == 1
    assert declined[0].note is None


def test_decline_run_records_reason_code_and_lesson(tmp_path):
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe")[0] == 0
    settings = _settings(tmp_path)
    decline_run(run, act=act, orc=FakeORCClient(), settings=settings)
    o = OutcomeStore(settings.outcome_path).latest(1)[0]
    assert o.status == "declined"
    assert o.reason_code == "unsafe"
    assert o.lessons[0] == DECLINED_LESSON
    assert o.lessons[1] == "decline reason: unsafe"
    declined_events = [e for e in act.events if e.kind == "run.declined"]
    assert len(declined_events) == 1
    assert declined_events[0].payload["reason_code"] == "unsafe"


def test_decline_run_keeps_reason_code_with_text(tmp_path):
    """End to end: CLI free text does not pollute the outcome's reason_code or lesson."""
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe", "touches", "auth")[0] == 0
    settings = _settings(tmp_path)
    decline_run(run, act=act, orc=FakeORCClient(), settings=settings)
    o = OutcomeStore(settings.outcome_path).latest(1)[0]
    assert o.status == "declined"
    assert o.reason_code == "unsafe"
    assert o.lessons[0] == DECLINED_LESSON
    assert o.lessons[1] == "decline reason: unsafe"
    declined_events = [e for e in act.events if e.kind == "run.declined"]
    assert len(declined_events) == 1
    assert declined_events[0].payload["reason_code"] == "unsafe"


def test_decline_run_without_reason_has_no_reason_code(tmp_path):
    act, run = _setup()
    assert _cli(act, "reject", run.id)[0] == 0
    settings = _settings(tmp_path)
    decline_run(run, act=act, orc=FakeORCClient(), settings=settings)
    o = OutcomeStore(settings.outcome_path).latest(1)[0]
    assert o.reason_code is None
    assert o.lessons == [DECLINED_LESSON]
    declined_events = [e for e in act.events if e.kind == "run.declined"]
    assert declined_events[0].payload["reason_code"] is None


def test_decline_reason_parsing_stops_at_separator():
    act, run = _setup()
    a = _pending(act, run)[0]
    act.decide_approval(a.id, ApprovalDecision.declined, actor="operator",
                        note="reason=scope_creep; free text here")
    assert _decline_reason(act, run) == "scope_creep"


def test_decline_reason_parses_code_before_colon():
    """B2: a note 'reason=CODE: free text' (CLI --reason CODE TEXT, Telegram
    'reject <name> CODE text', ACT's reason field) yields CODE: ':' is a stop like ';'
    and whitespace. Without this, 'reason=unsafe: touches auth' parsed as 'unsafe:'."""
    act, run = _setup()
    a = _pending(act, run)[0]
    act.decide_approval(a.id, ApprovalDecision.declined, actor="operator",
                        note="reason=unsafe: touches auth")
    assert _decline_reason(act, run) == "unsafe"
    act2, run2 = _setup()
    b = _pending(act2, run2)[0]
    act2.decide_approval(b.id, ApprovalDecision.declined, actor="operator", note="reason=unsafe:")
    assert _decline_reason(act2, run2) == "unsafe"


def test_decline_reason_ignores_notes_without_prefix_and_errors():
    act, run = _setup()
    a = _pending(act, run)[0]
    act.decide_approval(a.id, ApprovalDecision.declined, actor="operator", note="just a note")
    assert _decline_reason(act, run) is None

    class Broken:
        def list_approvals(self, **kw):
            raise RuntimeError("act down")

    assert _decline_reason(Broken(), run) is None


class _Resp:
    def raise_for_status(self):
        return None


class _Recorder:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        return _Resp()


def test_http_decide_approval_sends_note_only_when_given(monkeypatch):
    act = HTTPACTClient("http://localhost:9")
    rec = _Recorder()
    monkeypatch.setattr(act, "client", rec)
    act.decide_approval("approval-1", ApprovalDecision.declined, actor="operator")
    url, body = rec.calls[-1]
    assert url.endswith("/approvals/approval-1/decide")
    assert set(body) == {"decision", "actor"}
    assert body["decision"] == "declined" and body["actor"] == "operator"
    act.decide_approval("approval-1", ApprovalDecision.declined, actor="operator",
                        note="reason=unsafe")
    url, body = rec.calls[-1]
    assert set(body) == {"decision", "actor", "note"}
    assert body["note"] == "reason=unsafe"


def test_outcome_line_without_reason_code_still_loads(tmp_path):
    path = tmp_path / "old.jsonl"
    old_line = json.dumps({"id": "outcome-1", "schema_version": 1, "run_id": "run-1",
                           "proposal_id": "p", "status": "declined", "summary": "s",
                           "lessons": [DECLINED_LESSON]})
    path.write_text(old_line + "\n")
    outcomes = OutcomeStore(path)._read_all()
    assert len(outcomes) == 1
    assert outcomes[0].reason_code is None
    assert outcomes[0].run_id == "run-1"
