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


def test_reject_reason_code_is_case_insensitive():
    """Housekeeping 2026-11-10: the code is matched lowercased (Telegram already lowercases it);
    the free text after it keeps its case; a refusal still quotes what was typed."""
    act, run = _setup()
    rc, out = _cli(act, "reject", run.id, "--reason", "Unsafe")
    assert rc == 0, out
    assert _declined(act, run)[0].note == "reason=unsafe"
    act2, run2 = _setup()
    rc, out = _cli(act2, "reject", run2.id, "--reason", "UNSAFE:", "Touches", "Auth")
    assert rc == 0, out
    assert _declined(act2, run2)[0].note == "reason=unsafe: Touches Auth"
    act3, run3 = _setup()
    rc, out = _cli(act3, "reject", run3.id, "--reason", "Bogus")
    assert rc == 2 and "'Bogus'" in out
    assert len(_pending(act3, run3)) == 1 and _declined(act3, run3) == []


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


# ---- reviewer memory B1: the decline writer ------------------------------------------------

def _mem_settings(tmp_path, on=True):
    return SimpleNamespace(outcome_path=str(tmp_path / "o.jsonl"), repo_memory=on,
                           repo_memory_root=str(tmp_path / "mem"))


def _mem_entries(tmp_path):
    path = tmp_path / "mem" / "r.jsonl"          # repo "/r" -> basename r
    assert path.is_file(), "decline was not remembered"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_decline_run_remembers_reason_note_and_files(tmp_path):
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe")[0] == 0
    settings = _mem_settings(tmp_path)
    out_run = decline_run(run, act=act, orc=FakeORCClient(), settings=settings)
    assert out_run.status.value == "aborted"
    entries = _mem_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "decline"
    assert e["reason_code"] == "unsafe"
    assert e["text"] == "reason=unsafe"             # today's note; B2 adds free text
    assert e["files"] == ["a.py"]
    assert e["source"] == "operator"
    assert e["repo"] == "/r" and e["title"] == "t"
    assert e["run_id"] == run.id and e["proposal_id"] == run.proposal_id
    assert e["base_sha"] == "a" * 40 and e["verified_sha"] is None
    # the outcome, its lessons and the run.declined payload are exactly as before
    o = OutcomeStore(settings.outcome_path).latest(1)[0]
    assert o.status == "declined" and o.reason_code == "unsafe"
    assert o.lessons == [DECLINED_LESSON, "decline reason: unsafe"]
    declined_events = [ev for ev in act.events if ev.kind == "run.declined"]
    assert len(declined_events) == 1
    assert declined_events[0].payload == {"run_id": run.id, "proposal_id": run.proposal_id,
                                          "reason_code": "unsafe"}


def test_decline_from_telegram_without_reason_is_remembered_as_telegram(tmp_path):
    from navigator.observability.telegram_inbox import TELEGRAM_ACTOR
    act, run = _setup()
    a = _pending(act, run)[0]
    act.decide_approval(a.id, ApprovalDecision.declined, actor=TELEGRAM_ACTOR)   # no note
    decline_run(run, act=act, orc=FakeORCClient(), settings=_mem_settings(tmp_path))
    e = _mem_entries(tmp_path)[0]
    assert e["source"] == "telegram"
    assert e["reason_code"] is None
    assert e["text"] == "no reason given"
    assert e["files"] == ["a.py"]


def test_decline_with_unknown_actor_keeps_the_actor_string_and_never_the_word_none(tmp_path):
    act, run = _setup()
    a = _pending(act, run)[0]
    act.decide_approval(a.id, ApprovalDecision.declined, actor="act-ui", note="reason=low_value; meh")
    decline_run(run, act=act, orc=FakeORCClient(), settings=_mem_settings(tmp_path))
    e = _mem_entries(tmp_path)[0]
    assert e["source"] == "act-ui"
    assert e["reason_code"] == "low_value" and e["text"] == "reason=low_value; meh"
    # an approval whose actor is missing maps to the operator, never to the string "None"
    act2, run2 = _setup()
    a2 = _pending(act2, run2)[0]
    act2.decide_approval(a2.id, ApprovalDecision.declined, actor=None)
    (tmp_path / "mem" / "r.jsonl").unlink()
    decline_run(run2, act=act2, orc=FakeORCClient(), settings=_mem_settings(tmp_path))
    e2 = _mem_entries(tmp_path)[0]
    assert e2["source"] == "operator" and "None" not in json.dumps(e2["source"])


def test_decline_run_memory_off_writes_nothing(tmp_path, monkeypatch):
    # guard: the plain _settings namespace (no repo_memory attribute) and env unset -> no file, no ACT lookup
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY", raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe")[0] == 0
    calls = []
    real = act.get_proposal
    act.get_proposal = lambda pid: (calls.append(pid), real(pid))[1]
    decline_run(run, act=act, orc=FakeORCClient(), settings=_settings(tmp_path))
    assert not (tmp_path / "mem").exists()
    assert calls == []                    # off = byte-identical: no extra ACT call
    decline_run(run, act=act, orc=FakeORCClient(), settings=_mem_settings(tmp_path, on=False))
    assert not (tmp_path / "mem").exists()


def test_decline_run_memory_failure_never_changes_the_outcome(tmp_path):
    from structlog.testing import capture_logs
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe")[0] == 0
    clash = tmp_path / "afile"; clash.write_text("x")
    settings = SimpleNamespace(outcome_path=str(tmp_path / "o.jsonl"), repo_memory=True,
                               repo_memory_root=str(clash / "sub"))
    with capture_logs() as logs:
        out_run = decline_run(run, act=act, orc=FakeORCClient(), settings=settings)   # must NOT raise
    assert out_run.status.value == "aborted"
    o = OutcomeStore(settings.outcome_path).latest(1)[0]
    assert o.status == "declined" and o.reason_code == "unsafe" and o.lessons[0] == DECLINED_LESSON
    assert len([ev for ev in act.events if ev.kind == "run.declined"]) == 1
    assert [l["kind"] for l in logs if l["event"] == "repo_memory.write_failed"] == ["decline"]


def test_decline_run_with_missing_proposal_skips_memory_with_a_warning(tmp_path):
    from structlog.testing import capture_logs
    act, run = _setup()
    assert _cli(act, "reject", run.id, "--reason", "unsafe")[0] == 0
    del act.proposals[run.proposal_id]
    settings = _mem_settings(tmp_path)
    with capture_logs() as logs:
        out_run = decline_run(run, act=act, orc=FakeORCClient(), settings=settings)   # must NOT raise
    assert out_run.status.value == "aborted"
    assert OutcomeStore(settings.outcome_path).latest(1)[0].reason_code == "unsafe"
    skipped = [l for l in logs if l["event"] == "repo_memory.decline_skipped"]
    assert len(skipped) == 1 and skipped[0]["run_id"] == run.id
    assert not (tmp_path / "mem").exists()


def test_decline_note_prefers_the_approval_that_carries_the_reason(tmp_path, monkeypatch):
    """Two declines on one run (a bare Telegram one, then a CLI --reason one): the memory entry takes
    the note and actor of the approval that carries the reason code (audit 2026-11-09)."""
    from navigator.runtime import executor as ex
    from navigator.runtime import repo_memory

    class A:
        def __init__(self, note, actor):
            self.note, self.actor = note, actor

    class Act:
        def list_approvals(self, **kw):
            return [A(None, "operator-telegram"), A("reason=unsafe: touches auth", "operator")]
    run = SimpleNamespace(id="run-2")
    assert ex._decline_note(Act(), run) == ("reason=unsafe: touches auth", "operator")

    class ActBare:
        def list_approvals(self, **kw):
            return [A(None, None), A("free text", "someone")]
    assert ex._decline_note(ActBare(), run) == (None, None)

    class ActBoom:
        def list_approvals(self, **kw):
            raise RuntimeError("down")
    assert ex._decline_note(ActBoom(), run) == (None, None)
    assert repo_memory.enabled(SimpleNamespace(repo_memory=False)) is False


def test_touched_files_runs_no_git_while_the_switch_is_off(monkeypatch):
    from navigator.runtime import executor as ex
    calls = []
    monkeypatch.setattr(ex, "_git_run", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout=""))
    prop = SimpleNamespace(affected_files=["b.py", "a.py"])
    assert ex._touched_files(prop, "/nowhere", "abc", SimpleNamespace(repo_memory=False)) == ["a.py", "b.py"]
    assert calls == []
    ex._touched_files(prop, "/nowhere", "abc", SimpleNamespace(repo_memory=True))
    assert len(calls) == 1
