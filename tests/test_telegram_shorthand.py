"""N8 part 2 (operator 2026-10-16: 'what is run id, just merge should work'): Telegram
accepts 'merge' / 'reject' and 'merge <name>' / 'reject <name>' without a run id.

Resolution happens at apply time against live ACT state with every check the full-id
form applies (awaiting_review, pending merge approval, audited). Every audited run here
is built exactly like tests/test_telegram_inbox.py::_run: verified_sha set and the
run.awaiting_review event recorded BEFORE `navigator audited`.
"""
import io
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from navigator.cli import is_audited, merge_tier_of, run_cli
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.event import Event
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.ids import new_id
from navigator.observability.telegram_inbox import (TELEGRAM_ACTOR, TelegramCommand, _label,
                                                    apply_commands, parse_command, short_name)
from tests.test_telegram_inbox import (VERIFIED_SHA, _Recorder, _decisions, _inbox, _merge,
                                       _update)

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _execute_mode(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    monkeypatch.delenv("NAVIGATOR_MERGE_IN_DRY_RUN", raising=False)
    # an exported value must not leak into the 'flag unset' merge-all cases
    monkeypatch.delenv("NAVIGATOR_MERGE_TIER_B_BATCH", raising=False)


def _run(act, title, audited=True, pending=True, phase=RunPhase.awaiting_review):
    n = next(_seq)
    prop = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash=f"hash-{n}",
                                        title=title, rationale="r", repo="/r",
                                        base_commit_sha="abc", intended_changes="x",
                                        affected_files=["a.py"]))
    run = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch=f"nav/{n}",
                             phase=phase, correlation_id=f"corr-{n}",
                             verified_sha=VERIFIED_SHA))
    act.emit_event(Event(kind="run.awaiting_review", correlation_id=f"corr-{n}",
                         ts=datetime.now(timezone.utc) - timedelta(seconds=1),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA}))
    if pending:
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    if audited:
        assert run_cli(["audited", run.id], act=act, out=io.StringIO()) == 0
        assert is_audited(act, run.id)
    return run


def _pending(act, run_id):
    return len(_merge(act, run_id, ApprovalDecision.pending))


def _approved(act, run_id):
    return _merge(act, run_id, ApprovalDecision.approved)


# ---- parsing -------------------------------------------------------------------

def test_short_name_is_first_title_token_without_colon():
    assert short_name("N5b: Telegram inbox") == "N5b"
    assert short_name("S3a squash merges") == "S3a"
    assert short_name("  N8: rev4  ") == "N8"
    assert short_name("") == ""


def test_bare_merge_and_reject_parse_without_run_id():
    for text in ("merge", "/merge", "/MERGE", "Merge", "  merge  "):
        assert parse_command(text, 1) == TelegramCommand("merge", None, 1), text
    for text in ("reject", "/reject", "Reject"):
        assert parse_command(text, 2) == TelegramCommand("reject", None, 2), text


def test_named_merge_parses_short_name():
    assert parse_command("merge N5b", 3) == TelegramCommand("merge", None, 3, name="N5b")
    assert parse_command("/MERGE n5b", 3) == TelegramCommand("merge", None, 3, name="n5b")
    assert parse_command("reject S3a", 4) == TelegramCommand("reject", None, 4, name="S3a")


def test_reject_with_reason_words_parses_note():
    """B2: the words after the reject target are the decline reason. A leading REASON_CODE
    (case-insensitive, trailing ':' tolerated) is the code; anything else is free text
    filed under 'other'. The target stays parts[1] (short name or full run id) with the
    same rules as before; no words -> note None."""
    cmd = parse_command("reject S3a unsafe touches auth", 4)
    assert cmd is not None
    assert cmd == TelegramCommand("reject", None, 4, name="S3a",
                                  note="reason=unsafe: touches auth")
    cmd = parse_command("reject S3a flaky on CI", 5)
    assert cmd is not None
    assert cmd == TelegramCommand("reject", None, 5, name="S3a",
                                  note="reason=other: flaky on CI")
    rid = new_id("run")
    cmd = parse_command(f"Reject {rid} Unsafe:", 6)
    assert cmd is not None
    assert cmd == TelegramCommand("reject", rid, 6, note="reason=unsafe")
    assert parse_command("/reject S3a tests_weak", 7) == TelegramCommand(
        "reject", None, 7, name="S3a", note="reason=tests_weak")
    assert parse_command("reject S3a  Duplicate:   of  N5b ", 8) == TelegramCommand(
        "reject", None, 8, name="S3a", note="reason=duplicate: of N5b")
    # no words: note None, so apply decides without a note (B1 renders 'no reason given')
    assert parse_command("reject S3a", 9) == TelegramCommand("reject", None, 9, name="S3a")
    assert parse_command("reject S3a", 9).note is None
    # the target rules are unchanged: a truncated run id or a bad name is not a command,
    # and bare 'reject' + reason is unsupported (tests_weak would be taken as a name)
    assert parse_command("reject run-01M unsafe", 10) is None
    assert parse_command("reject S3a? unsafe", 10) is None
    assert parse_command("reject tests_weak flaky", 11) == TelegramCommand(
        "reject", None, 11, name="tests_weak", note="reason=other: flaky")


def test_merge_with_extra_words_is_still_not_a_command():
    """GUARD (green on main): B2 widens only `reject`; 'merge <target> <words>' stays chat
    and 'reject all ...' is still refused by the batch branch."""
    rid = new_id("run")
    for text in ("merge S3a now", "merge S3a looks good", "/merge S3a unsafe",
                 "merge S3a unsafe touches auth", f"merge {rid} now", "reject all",
                 "reject all unsafe", "/reject all except N5b"):
        assert parse_command(text, 1) is None, text


def test_ordinary_chat_is_not_a_command():
    for text in ("merge looks good?", "merge?", "please merge", "merged", "merge it now",
                 "merge N5b\n", "merge\nN5b", "merge run-01M", "/merge run-01M", "", "   ",
                 "merge N5b?", "//merge"):
        assert parse_command(text, 1) is None, text


def test_full_id_form_unchanged():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    assert parse_command(f"/merge {run.id}", 5) == TelegramCommand("merge", run.id, 5)
    assert parse_command(f"merge {run.id}", 5) == TelegramCommand("merge", run.id, 5)
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", run.id, 5)], rec)
    assert results == [{"run_id": run.id, "action": "merge", "applied": True, "reason": None}]
    assert len(_approved(act, run.id)) == 1
    assert rec.sent == [f"Navigator: {run.id} merge approved from Telegram (applies this cycle)"]


# ---- bare merge ----------------------------------------------------------------

def test_bare_merge_with_one_candidate_applies(tmp_path):
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(7, "merge")]).poll_once()
    assert cmds == [TelegramCommand("merge", None, 7)]
    results = apply_commands(act, cmds, rec)
    assert results == [{"run_id": run.id, "action": "merge", "applied": True, "reason": None}]
    approved = _approved(act, run.id)
    assert len(approved) == 1 and approved[0].actor == TELEGRAM_ACTOR
    assert _pending(act, run.id) == 0
    ev = _decisions(act)
    assert len(ev) == 1
    assert ev[0].payload == {"run_id": run.id, "decision": "approved", "update_id": 7}
    assert rec.sent == [f"Navigator: N5b - N5b: Telegram inbox ({run.id}) merge approved "
                        f"from Telegram (applies this cycle)"]
    assert run.id in rec.sent[0] and "N5b: Telegram inbox" in rec.sent[0]


def test_bare_reject_with_one_candidate_declines():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("reject", None, 2)], rec)
    assert results[0]["applied"] is True and results[0]["run_id"] == run.id
    declined = _merge(act, run.id, ApprovalDecision.declined)
    assert len(declined) == 1 and declined[0].actor == TELEGRAM_ACTOR
    assert _decisions(act)[0].payload["decision"] == "rejected"
    assert rec.sent == [f"Navigator: N5b - N5b: Telegram inbox ({run.id}) merge rejected "
                        f"from Telegram (applies this cycle)"]


def test_bare_merge_with_two_candidates_applies_nothing_and_lists_both():
    act = FakeACTClient()
    a = _run(act, "N5b: Telegram inbox")
    b = _run(act, "S3a: squash merges")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "ambiguous: several runs waiting for merge"}]
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    assert _approved(act, a.id) == [] and _approved(act, b.id) == []
    assert _decisions(act) == []
    assert len(rec.sent) == 1
    lines = rec.sent[0].splitlines()
    assert "N5b - N5b: Telegram inbox - reply: merge N5b" in lines
    assert "S3a - S3a: squash merges - reply: merge S3a" in lines
    assert "not applied" in lines[0]


def test_bare_merge_with_zero_candidates_replies_nothing_waiting():
    act = FakeACTClient()
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "nothing waiting for merge"}]
    assert act.events == []
    assert rec.sent == ["Navigator: /merge - nothing waiting for merge"]


def test_bare_merge_resolves_against_live_state_per_command(tmp_path):
    # the first 'merge' consumes the only candidate; the second finds nothing waiting
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(1, "Merge"), _update(2, "/MERGE"),
                             _update(3, "merge looks good?")]).poll_once()
    assert cmds == [TelegramCommand("merge", None, 1), TelegramCommand("merge", None, 2)]
    results = apply_commands(act, cmds, rec)
    assert [r["applied"] for r in results] == [True, False]
    assert results[1]["reason"] == "nothing waiting for merge"
    assert len(_approved(act, run.id)) == 1
    assert len(_decisions(act)) == 1 and _decisions(act)[0].payload["update_id"] == 1


# ---- merge <name> --------------------------------------------------------------

def test_named_merge_resolves_by_short_name_case_insensitively(tmp_path):
    act = FakeACTClient()
    a = _run(act, "N5b: Telegram inbox")
    b = _run(act, "S3a: squash merges")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(4, "merge n5b")]).poll_once()
    assert cmds == [TelegramCommand("merge", None, 4, name="n5b")]
    results = apply_commands(act, cmds, rec)
    assert results == [{"run_id": a.id, "action": "merge", "applied": True, "reason": None}]
    assert len(_approved(act, a.id)) == 1 and _pending(act, a.id) == 0
    assert _pending(act, b.id) == 1 and _approved(act, b.id) == []
    assert rec.sent == [f"Navigator: N5b - N5b: Telegram inbox ({a.id}) merge approved "
                        f"from Telegram (applies this cycle)"]
    results = apply_commands(act, [TelegramCommand("reject", None, 5, name="S3A")], rec)
    assert results[0]["applied"] is True and results[0]["run_id"] == b.id
    assert len(_merge(act, b.id, ApprovalDecision.declined)) == 1


def test_named_reject_with_reason_records_note_and_says_so(tmp_path):
    """B2: the note reaches ACT's approval (B1's decline_run writer reads it), the reply
    echoes it after the event note, and the run.telegram_decision payload is unchanged."""
    act = FakeACTClient()
    run = _run(act, "S3a: squash merges")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(6, "reject S3a unsafe touches auth")]).poll_once()
    assert len(cmds) == 1
    assert cmds == [TelegramCommand("reject", None, 6, name="S3a",
                                    note="reason=unsafe: touches auth")]
    results = apply_commands(act, cmds, rec)
    assert results == [{"run_id": run.id, "action": "reject", "applied": True, "reason": None}]
    declined = _merge(act, run.id, ApprovalDecision.declined)
    assert len(declined) == 1 and declined[0].actor == TELEGRAM_ACTOR
    assert declined[0].note == "reason=unsafe: touches auth"
    assert _pending(act, run.id) == 0
    ev = _decisions(act)
    assert len(ev) == 1
    assert ev[0].payload == {"run_id": run.id, "decision": "rejected", "update_id": 6}
    assert rec.sent == [f"Navigator: S3a - S3a: squash merges ({run.id}) merge rejected "
                        f"from Telegram (applies this cycle) - reason=unsafe: touches auth"]


def test_reject_without_reason_passes_no_note_kwarg():
    """A note-less ACT (tests/support.py ProtocolOnlyACT shape: decide_approval without
    `note`) still takes a bare reject; the note kwarg is only passed when there is one."""
    class _NoteLessACT(FakeACTClient):
        def decide_approval(self, approval_id, decision, actor):
            return FakeACTClient.decide_approval(self, approval_id, decision, actor)

    act = _NoteLessACT()
    run = _run(act, "S3a: squash merges")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("reject", None, 1, name="S3a")], rec)
    assert results[0]["applied"] is True and results[0]["run_id"] == run.id
    declined = _merge(act, run.id, ApprovalDecision.declined)
    assert len(declined) == 1 and declined[0].note is None
    assert rec.sent == [f"Navigator: S3a - S3a: squash merges ({run.id}) merge rejected "
                        f"from Telegram (applies this cycle)"]


def test_reasoned_reject_with_unknown_name_is_chat_not_a_command(tmp_path):
    """B2 guard: SHORT_NAME accepts any word, so 'reject this one please' parses as a
    reasoned reject of 'this'. When the name matches no waiting run it is operator chat:
    no reply, no decision, result reason 'not a command'."""
    act = FakeACTClient()
    run = _run(act, "S3a: squash merges")
    rec = _Recorder()
    cmd = parse_command("reject this one please", 7)
    assert cmd is not None and cmd.name == "this" and cmd.note
    results = apply_commands(act, [cmd], rec)
    assert results == [{"run_id": None, "action": "reject", "applied": False,
                        "reason": "not a command"}]
    assert rec.sent == []
    assert _pending(act, run.id) == 1 and _decisions(act) == []
    assert _merge(act, run.id, ApprovalDecision.declined) == []


def test_reasoned_reject_with_unknown_name_falls_through_to_chat_in_handle_operator_message():
    """The bot forwards a False to the conductor chat; True would swallow the line."""
    from navigator.observability.telegram_proposals import handle_operator_message
    from tests.test_actions import CHAT, NOW, _msg

    act = FakeACTClient()
    run = _run(act, "S3a: squash merges")
    rec = _Recorder()
    assert handle_operator_message(_msg("reject this one please"), act, rec, CHAT,
                                   now=NOW, update_id=8) is False
    assert rec.sent == []
    assert _pending(act, run.id) == 1 and _decisions(act) == []


def test_bare_reject_with_unknown_name_still_lists_candidates():
    """No note: 'reject nosuch' keeps the candidate reply (the operator typed a target)."""
    act = FakeACTClient()
    run = _run(act, "S3a: squash merges")
    rec = _Recorder()
    results = apply_commands(act, [parse_command("reject nosuch", 9)], rec)
    assert results == [{"run_id": None, "action": "reject", "applied": False,
                        "reason": "no single run named 'nosuch'"}]
    assert len(rec.sent) == 1 and rec.sent[0].startswith("Navigator: /reject nosuch not applied")
    assert "S3a - S3a: squash merges - reply: reject S3a" in rec.sent[0].splitlines()
    assert _pending(act, run.id) == 1


# ---- B4: 'fix' - one rebuild for a declined run, carrying its reason ---------------

def _declined(act, title, note=None, ended=None, status=None):
    """A proposal (approved unless `status`) with one declined run: aborted, ended at
    `ended` (default now), a declined merge approval carrying `note`. Returns the run."""
    from navigator.contracts.enums import ProposalStatus, RunStatus
    n = next(_seq)
    prop = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash=f"hash-{n}",
                                        title=title, rationale="r", repo="/r",
                                        base_commit_sha="abc", intended_changes="x",
                                        affected_files=["a.py"],
                                        status=status or ProposalStatus.approved))
    ended = ended or datetime.now(timezone.utc)
    run = act.create_run(Run(proposal_id=prop.id, base_commit_sha="abc", branch=f"nav/{n}",
                             status=RunStatus.aborted, phase=RunPhase.finalizing,
                             correlation_id=f"corr-{n}", started_at=ended - timedelta(hours=1),
                             ended_at=ended))
    appr = act.create_approval(target=run.id, gate=ApprovalGate.merge)
    act.decide_approval(appr.id, ApprovalDecision.declined, actor=TELEGRAM_ACTOR,
                        **({"note": note} if note else {}))
    return run


def _grants(act, proposal_id):
    return act.list_approvals(target=proposal_id, gate=ApprovalGate.review,
                              decision=ApprovalDecision.approved)


def _fix_events(act):
    return [e for e in act.events if e.kind == "run.fix_requested"]


def test_fix_parses_like_merge():
    rid = new_id("run")
    for text in ("fix", "/fix", "/FIX", "Fix", "  fix  "):
        assert parse_command(text, 1) == TelegramCommand("fix", None, 1), text
    assert parse_command("fix S3a", 2) == TelegramCommand("fix", None, 2, name="S3a")
    assert parse_command("/FIX n5b", 3) == TelegramCommand("fix", None, 3, name="n5b")
    assert parse_command(f"fix {rid}", 4) == TelegramCommand("fix", rid, 4)
    assert parse_command(f"/fix {rid}", 5) == TelegramCommand("fix", rid, 5)
    assert parse_command("fix S3a", 2).note is None
    for text in ("fix S3a now", "fix all", "fix all except N5b", "fix run-01M", "/fix run-01M",
                 "fix S3a?", "fix it please", f"fix {rid} now", "fix\nS3a", "fixed", "fix?"):
        assert parse_command(text, 6) is None, text
    assert _label(TelegramCommand("fix", None, 1, name="S3a")) == "/fix S3a"
    assert _label(TelegramCommand("fix", None, 1)) == "/fix"
    assert _label(TelegramCommand("fix", rid, 1)) == f"/fix {rid}"


def test_named_fix_grants_one_rebuild_and_says_so(tmp_path):
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges", note="reason=unsafe: touches auth")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(6, "fix s3a")]).poll_once()
    assert cmds == [TelegramCommand("fix", None, 6, name="s3a")]
    results = apply_commands(act, cmds, rec)
    assert results == [{"run_id": run.id, "action": "fix", "applied": True, "reason": None}]
    grants = _grants(act, run.proposal_id)
    assert len(grants) == 1 and grants[0].actor == TELEGRAM_ACTOR
    assert grants[0].note == f"fix {run.id}"
    (ev,) = _fix_events(act)
    assert ev.correlation_id == run.correlation_id
    assert ev.payload == {"run_id": run.id, "proposal_id": run.proposal_id,
                          "actor": TELEGRAM_ACTOR, "update_id": 6}
    assert _decisions(act) == []  # not a merge decision
    assert rec.sent == [f"Navigator: S3a - S3a: squash merges ({run.id}) fix requested - one "
                        "rebuild granted, decline reason on record: reason=unsafe: touches auth; "
                        "builds in execute mode"]
    # the declined merge approval is untouched and the proposal stays approved
    assert len(_merge(act, run.id, ApprovalDecision.declined)) == 1
    from navigator.contracts.enums import ProposalStatus
    assert act.get_proposal(run.proposal_id).status == ProposalStatus.approved


def test_repeated_fix_is_already_queued_and_grants_nothing_more():
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("fix", None, 1, name="S3a")], rec)
    assert len(_grants(act, run.proposal_id)) == 1
    assert rec.sent[0].endswith("decline reason on record: no reason given; builds in execute mode")
    results = apply_commands(act, [TelegramCommand("fix", None, 2, name="S3a")], rec)
    assert results == [{"run_id": run.id, "action": "fix", "applied": False,
                        "reason": "already queued"}]
    assert rec.sent[1] == "Navigator: /fix S3a not applied - already queued"
    assert len(_grants(act, run.proposal_id)) == 1 and len(_fix_events(act)) == 1
    # the run-id form hits the same guard
    results = apply_commands(act, [TelegramCommand("fix", run.id, 3)], rec)
    assert results[0]["applied"] is False and results[0]["reason"] == "already queued"
    assert rec.sent[2] == f"Navigator: /fix {run.id} not applied - already queued"
    assert len(_grants(act, run.proposal_id)) == 1


def test_bare_fix_with_one_candidate_applies_and_with_two_lists_both():
    act = FakeACTClient()
    a = _declined(act, "N5b: Telegram inbox")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("fix", None, 1)], rec)
    assert results == [{"run_id": a.id, "action": "fix", "applied": True, "reason": None}]
    assert len(_grants(act, a.proposal_id)) == 1
    assert rec.sent[0].startswith(f"Navigator: N5b - N5b: Telegram inbox ({a.id}) fix requested")
    act2 = FakeACTClient()
    a = _declined(act2, "N5b: Telegram inbox", ended=datetime.now(timezone.utc) - timedelta(hours=1))
    b = _declined(act2, "S3a: squash merges")
    rec = _Recorder()
    results = apply_commands(act2, [TelegramCommand("fix", None, 2)], rec)
    assert results == [{"run_id": None, "action": "fix", "applied": False,
                        "reason": "ambiguous: several declined runs"}]
    assert _grants(act2, a.proposal_id) == [] and _grants(act2, b.proposal_id) == []
    assert _fix_events(act2) == []
    lines = rec.sent[0].splitlines()
    assert lines[0] == "Navigator: /fix not applied - ambiguous: several declined runs; 2 declined:"
    assert lines[1:] == ["S3a - S3a: squash merges - reply: fix S3a",
                         "N5b - N5b: Telegram inbox - reply: fix N5b"]  # newest first


def test_bare_fix_and_run_id_fix_with_nothing_declined_reply():
    act = FakeACTClient()
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("fix", None, 1)], rec)
    assert results == [{"run_id": None, "action": "fix", "applied": False,
                        "reason": "nothing declined"}]
    assert rec.sent == ["Navigator: /fix - nothing declined in the last 7 days"]
    rid = new_id("run")
    results = apply_commands(act, [TelegramCommand("fix", rid, 2)], rec)
    assert results == [{"run_id": rid, "action": "fix", "applied": False,
                        "reason": "nothing declined"}]
    assert rec.sent[1] == f"Navigator: /fix {rid} - nothing declined in the last 7 days"


def test_fix_by_run_id_not_a_candidate_lists_candidates():
    """The run-id form is always a command: an unknown, old or rebuilt run gets the list."""
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()
    rid = new_id("run")
    results = apply_commands(act, [TelegramCommand("fix", rid, 1)], rec)
    assert results == [{"run_id": rid, "action": "fix", "applied": False,
                        "reason": "not a declined run"}]
    assert rec.sent[0].splitlines() == [
        f"Navigator: /fix {rid} not applied - not a declined run from the last 7 days; 1 declined:",
        "S3a - S3a: squash merges - reply: fix S3a"]
    assert _grants(act, run.proposal_id) == []
    # the exact id applies
    results = apply_commands(act, [TelegramCommand("fix", run.id, 2)], rec)
    assert results == [{"run_id": run.id, "action": "fix", "applied": True, "reason": None}]
    assert rec.sent[1].startswith(f"Navigator: S3a - S3a: squash merges ({run.id}) fix requested")


def test_fix_with_unknown_name_is_chat_not_a_command():
    """Amendment 2: 'fix it' / 'fix tests' name no declined run -> no reply, result
    'not a command', so handle_operator_message hands the line to chat."""
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()
    for text in ("fix it", "fix tests", "fix nosuch"):
        cmd = parse_command(text, 7)
        assert cmd is not None and cmd.name is not None
        results = apply_commands(act, [cmd], rec)
        assert results == [{"run_id": None, "action": "fix", "applied": False,
                            "reason": "not a command"}], text
    assert rec.sent == []
    assert _grants(act, run.proposal_id) == [] and _fix_events(act) == []
    # with nothing declined at all a named fix is chat too
    results = apply_commands(FakeACTClient(), [parse_command("fix S3a", 8)], rec)
    assert results[0]["reason"] == "not a command" and rec.sent == []


def test_fix_with_unknown_name_falls_through_to_chat_in_handle_operator_message():
    """Mirror of the B2 fall-through: False, no reply, nothing granted."""
    from navigator.observability.telegram_proposals import handle_operator_message
    from tests.test_actions import CHAT, NOW, _msg

    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()
    assert handle_operator_message(_msg("fix it"), act, rec, CHAT, now=NOW, update_id=8) is False
    assert rec.sent == []
    assert _grants(act, run.proposal_id) == [] and _fix_events(act) == []
    # the real name through the same handler is a command and applies
    assert handle_operator_message(_msg("fix S3a"), act, rec, CHAT, now=NOW, update_id=9) is True
    assert len(_grants(act, run.proposal_id)) == 1
    assert _fix_events(act)[0].payload["update_id"] == 9
    assert rec.sent[0].startswith(f"Navigator: S3a - S3a: squash merges ({run.id}) fix requested")


def test_ambiguous_fix_name_lists_and_grants_nothing():
    act = FakeACTClient()
    a = _declined(act, "S3a: first", ended=datetime.now(timezone.utc) - timedelta(hours=1))
    b = _declined(act, "S3a: second")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("fix", None, 1, name="s3a")], rec)
    assert results == [{"run_id": None, "action": "fix", "applied": False,
                        "reason": "no single declined run named 's3a'"}]
    assert _grants(act, a.proposal_id) == [] and _grants(act, b.proposal_id) == []
    lines = rec.sent[0].splitlines()
    assert lines[0] == ("Navigator: /fix s3a not applied - no single declined run named 's3a'; "
                        "2 declined:")
    assert set(lines[1:]) == {f"S3a - S3a: second - reply: fix {b.id}",
                              f"S3a - S3a: first - reply: fix {a.id}"}


def test_fix_refused_for_a_declined_proposal_and_a_rebuilt_run():
    from navigator.contracts.enums import ProposalStatus, RunStatus
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges", status=ProposalStatus.declined)
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("fix", None, 1, name="S3a")], rec)
    assert results == [{"run_id": run.id, "action": "fix", "applied": False,
                        "reason": "proposal is declined"}]
    assert rec.sent == ["Navigator: /fix S3a not applied - proposal is declined"]
    assert _grants(act, run.proposal_id) == []
    # a rebuild already running makes the declined run no candidate at all
    act2 = FakeACTClient()
    run = _declined(act2, "N5b: Telegram inbox")
    act2.create_run(Run(proposal_id=run.proposal_id, base_commit_sha="abc", branch="nav/again",
                        status=RunStatus.executing, phase=RunPhase.building))
    rec = _Recorder()
    results = apply_commands(act2, [TelegramCommand("fix", run.id, 2)], rec)
    assert results[0]["applied"] is False and results[0]["reason"] == "nothing declined"
    assert _grants(act2, run.proposal_id) == []


def test_fix_apply_failure_replies_with_class_name_only(monkeypatch):
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()

    def boom(target, gate):
        raise RuntimeError("https://api.telegram.org/botSECRET")

    monkeypatch.setattr(act, "create_approval", boom)
    results = apply_commands(act, [TelegramCommand("fix", None, 1, name="S3a")], rec)
    assert results == [{"run_id": None, "action": "fix", "applied": False,
                        "reason": "error: RuntimeError"}]
    assert rec.sent == ["Navigator: could not apply /fix S3a: RuntimeError - re-send it"]
    assert _grants(act, run.proposal_id) == []


def test_fix_event_failure_still_reports_the_grant(monkeypatch):
    """Amendment 8: the grant is written; a failing event log never says 're-send'."""
    act = FakeACTClient()
    run = _declined(act, "S3a: squash merges")
    rec = _Recorder()
    real = act.emit_event

    def flaky(event):
        if event.kind == "run.fix_requested":
            raise RuntimeError("ACT down")
        return real(event)

    monkeypatch.setattr(act, "emit_event", flaky)
    results = apply_commands(act, [TelegramCommand("fix", None, 1, name="S3a")], rec)
    assert results == [{"run_id": run.id, "action": "fix", "applied": True, "reason": None}]
    assert len(_grants(act, run.proposal_id)) == 1
    assert len(rec.sent) == 1 and "re-send" not in rec.sent[0]
    assert rec.sent[0].startswith(f"Navigator: S3a - S3a: squash merges ({run.id}) fix requested")


def test_help_names_the_fix_word():
    from navigator.observability.telegram_proposals import HELP
    assert "fix N5b - rebuild a declined run with its reason" in HELP


def test_named_merge_without_colon_in_title():
    act = FakeACTClient()
    run = _run(act, "S3a squash merges for public repos")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, name="s3a")], rec)
    assert results[0]["applied"] is True and results[0]["run_id"] == run.id


def test_unknown_name_applies_nothing_and_lists_candidates():
    act = FakeACTClient()
    a = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, name="X9")], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "no single run named 'X9'"}]
    assert _pending(act, a.id) == 1 and _decisions(act) == []
    assert len(rec.sent) == 1
    assert rec.sent[0].startswith("Navigator: /merge X9 not applied")
    assert "N5b - N5b: Telegram inbox - reply: merge N5b" in rec.sent[0].splitlines()


def test_ambiguous_name_applies_nothing():
    act = FakeACTClient()
    a = _run(act, "N5b: Telegram inbox")
    b = _run(act, "N5b: Telegram inbox rev2")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, name="N5b")], rec)
    assert results[0]["applied"] is False and results[0]["run_id"] is None
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    assert _decisions(act) == []
    lines = rec.sent[0].splitlines()
    assert "N5b - N5b: Telegram inbox - reply: merge N5b" in lines
    assert "N5b - N5b: Telegram inbox rev2 - reply: merge N5b" in lines


def test_named_merge_with_no_candidates_replies_nothing_waiting():
    act = FakeACTClient()
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, name="N5b")], rec)
    assert results[0]["reason"] == "nothing waiting for merge"
    assert rec.sent == ["Navigator: /merge N5b - nothing waiting for merge"]


# ---- shorthand never skips a check ---------------------------------------------

def test_unaudited_run_is_never_resolvable():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox", audited=False)
    rec = _Recorder()
    for cmd in (TelegramCommand("merge", None, 1), TelegramCommand("merge", None, 2, name="N5b"),
                TelegramCommand("reject", None, 3)):
        results = apply_commands(act, [cmd], rec)
        assert results[0]["applied"] is False
        assert results[0]["reason"] == "nothing waiting for merge"
    assert _pending(act, run.id) == 1 and _approved(act, run.id) == []
    assert _decisions(act) == []
    assert all("nothing waiting for merge" in s for s in rec.sent)
    # the full-id form on the same run also refuses
    results = apply_commands(act, [TelegramCommand("merge", run.id, 4)], rec)
    assert results[0]["applied"] is False and "not audited yet" in rec.sent[-1]


def test_unaudited_sibling_does_not_make_bare_merge_ambiguous():
    act = FakeACTClient()
    good = _run(act, "N5b: Telegram inbox")
    _run(act, "S3a: squash merges", audited=False)
    _run(act, "S4: no approval", pending=False)
    _run(act, "S5: still building", phase=RunPhase.building, audited=False)
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results == [{"run_id": good.id, "action": "merge", "applied": True, "reason": None}]
    assert len(_approved(act, good.id)) == 1


def test_shorthand_apply_failure_replies_with_class_name_only():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")

    def boom(*args, **kwargs):
        raise ConnectionError("ACT down at http://act.local/botSECRETTOKEN")
    act.decide_approval = boom
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, name="N5b")], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "error: ConnectionError"}]
    assert rec.sent == ["Navigator: could not apply /merge N5b: ConnectionError - re-send it"]
    assert "SECRETTOKEN" not in rec.sent[0]
    assert _pending(act, run.id) == 1


def test_dry_run_with_merge_flag_says_applies_this_cycle(monkeypatch):
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    monkeypatch.setenv("NAVIGATOR_MODE", "dry_run")
    monkeypatch.setenv("NAVIGATOR_MERGE_IN_DRY_RUN", "1")
    apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert rec.sent == [f"Navigator: N5b - N5b: Telegram inbox ({run.id}) merge approved "
                        f"from Telegram (applies this cycle)"]
    monkeypatch.setenv("NAVIGATOR_MERGE_IN_DRY_RUN", "0")
    other = _run(act, "S3a: squash merges")
    apply_commands(act, [TelegramCommand("merge", None, 2)], rec)
    assert rec.sent[-1] == (f"Navigator: S3a - S3a: squash merges ({other.id}) merge approved "
                            f"from Telegram (recorded; merges apply only in execute mode)")


# ---- N8b: replies name the run; listing tolerates vanished proposals -----------

def test_full_id_reply_does_not_name_the_proposal():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert rec.sent == [f"Navigator: {run.id} merge approved from Telegram (applies this cycle)"]
    assert "N5b" not in rec.sent[0]


def test_vanished_proposal_is_listed_by_run_id_and_others_still_resolve():
    act = FakeACTClient()
    a = _run(act, "N5b: Telegram inbox")
    b = _run(act, "S3a: squash merges")
    real = act.get_proposal

    def flaky(proposal_id):
        if proposal_id == b.proposal_id:
            raise KeyError("proposal gone: http://act.local/botSECRETTOKEN")
        return real(proposal_id)
    act.get_proposal = flaky
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results[0]["applied"] is False
    assert results[0]["reason"] == "ambiguous: several runs waiting for merge"
    lines = rec.sent[0].splitlines()
    assert "N5b - N5b: Telegram inbox - reply: merge N5b" in lines
    assert f"{b.id} - {b.id} - reply: merge {b.id}" in lines
    assert "SECRETTOKEN" not in rec.sent[0]
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    # the other candidate is still resolvable by name
    results = apply_commands(act, [TelegramCommand("merge", None, 2, name="N5b")], rec)
    assert results[0] == {"run_id": a.id, "action": "merge", "applied": True, "reason": None}
    assert rec.sent[-1] == (f"Navigator: N5b - N5b: Telegram inbox ({a.id}) merge approved "
                            f"from Telegram (applies this cycle)")
    # and the vanished one by its full id
    results = apply_commands(act, [TelegramCommand("merge", b.id, 3)], rec)
    assert results[0]["applied"] is True
    assert rec.sent[-1] == f"Navigator: {b.id} merge approved from Telegram (applies this cycle)"


def test_vanished_proposal_as_sole_candidate_still_merges_by_bare_merge():
    act = FakeACTClient()
    run = _run(act, "N5b: Telegram inbox")

    def gone(proposal_id):
        raise RuntimeError("gone")
    act.get_proposal = gone
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results[0] == {"run_id": run.id, "action": "merge", "applied": True, "reason": None}
    assert rec.sent == [f"Navigator: {run.id} - {run.id} ({run.id}) merge approved "
                        f"from Telegram (applies this cycle)"]


def test_non_ascii_short_name_lists_run_id_to_reply_with():
    act = FakeACTClient()
    a = _run(act, "\u00d15: accents in title")
    b = _run(act, "S3a: squash merges")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    assert results[0]["applied"] is False
    lines = rec.sent[0].splitlines()
    assert f"\u00d15 - \u00d15: accents in title - reply: merge {a.id}" in lines
    assert "S3a - S3a: squash merges - reply: merge S3a" in lines
    # the suggested reply is a command parse_command accepts; the name itself is not
    assert parse_command(f"merge {a.id}", 2) == TelegramCommand("merge", a.id, 2)
    assert parse_command("merge \u00d15", 2) is None
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1


def test_run_like_short_name_lists_run_id_to_reply_with():
    act = FakeACTClient()
    a = _run(act, "run-01 fix the thing")
    _run(act, "S3a: squash merges")
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("merge", None, 1)], rec)
    lines = rec.sent[0].splitlines()
    assert f"run-01 - run-01 fix the thing - reply: merge {a.id}" in lines


# ---- 3c: merge all / merge all except ... ---------------------------------------

BATCH_OFF = ("Navigator: merge all is off - turn on 'merge all (tier A/B)' in Navigator settings "
             "(http://127.0.0.1:3000/navigator/settings), or set NAVIGATOR_MERGE_TIER_B_BATCH=1")


def test_batch_off_text_points_at_settings_page():
    assert "navigator/settings" in BATCH_OFF
    assert "merge all (tier A/B)" in BATCH_OFF
    assert BATCH_OFF.startswith("Navigator: merge all is off")


FULL_ID = "run-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _tiered(act, title, tier):
    """An audited candidate whose LATEST run.awaiting_review event records merge_tier.
    The tier event is emitted before `navigator audited` (is_audited needs the audit
    strictly after the latest awaiting_review event)."""
    run = _run(act, title, audited=False)
    act.emit_event(Event(kind="run.awaiting_review", correlation_id=run.correlation_id,
                         ts=datetime.now(timezone.utc) - timedelta(seconds=0.5),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA,
                                  "merge_tier": tier}))
    assert run_cli(["audited", run.id], act=act, out=io.StringIO()) == 0
    assert is_audited(act, run.id)
    assert merge_tier_of(act, run) == tier
    return run


def test_merge_all_parses():
    assert parse_command("merge all", 1) == TelegramCommand("merge", None, 1, batch=True)
    assert parse_command("/merge ALL", 1) == TelegramCommand("merge", None, 1, batch=True)
    assert parse_command("MERGE ALL except N5b,S3a", 2) == TelegramCommand(
        "merge", None, 2, batch=True, exclude=("N5b", "S3a"))
    assert parse_command("merge all except N5b S3a", 2) == TelegramCommand(
        "merge", None, 2, batch=True, exclude=("N5b", "S3a"))
    assert parse_command("merge all except N5b, S3a", 2) == TelegramCommand(
        "merge", None, 2, batch=True, exclude=("N5b", "S3a"))
    assert parse_command(f"merge all except {FULL_ID}", 2) == TelegramCommand(
        "merge", None, 2, batch=True, exclude=(FULL_ID,))
    for text in ("reject all", "/reject all except N5b", "merge all except", "merge,",
                 "merge,N5b", "merge all except run-01ABC", "merge all now",
                 "merge all except ,", "merge all except N5b?", f"{FULL_ID},"):
        assert parse_command(text, 3) is None, text
    # the single-run forms are unchanged
    assert parse_command("merge N5b", 1) == TelegramCommand("merge", None, 1, name="N5b")
    assert parse_command("merge", 1) == TelegramCommand("merge", None, 1)
    assert parse_command(f"merge {FULL_ID}", 1) == TelegramCommand("merge", FULL_ID, 1)


def test_label_for_batch():
    assert _label(TelegramCommand("merge", None, 1, batch=True)) == "/merge all"
    assert _label(TelegramCommand("merge", None, 1, batch=True,
                                  exclude=("a", "b"))) == "/merge all except a,b"
    assert _label(TelegramCommand("merge", None, 1, name="N5b")) == "/merge N5b"


def test_merge_all_approves_tier_a_and_b_and_lists_c(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "D1: docs", "A")
    c = _tiered(act, "T9: trader exp", "C")
    u = _run(act, "U1: untiered")  # audited, but no tier recorded
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(7, "merge all")]).poll_once()
    assert cmds == [TelegramCommand("merge", None, 7, batch=True)]
    results = apply_commands(act, cmds, rec)
    assert len(results) == 1
    assert results[0]["applied"] is True and results[0]["reason"] is None
    assert sorted(results[0]["approved"]) == sorted([a.id, b.id])
    assert _pending(act, a.id) == 0 and _pending(act, b.id) == 0
    assert _pending(act, c.id) == 1 and _approved(act, c.id) == []
    assert _pending(act, u.id) == 1 and _approved(act, u.id) == []
    assert len(_approved(act, a.id)) == 1 and len(_approved(act, b.id)) == 1
    assert all(ap.actor == TELEGRAM_ACTOR for ap in _approved(act, a.id) + _approved(act, b.id))
    ev = _decisions(act)
    assert len(ev) == 2
    assert sorted(e.payload["run_id"] for e in ev) == sorted([a.id, b.id])
    for e in ev:
        assert e.payload == {"run_id": e.payload["run_id"], "decision": "approved",
                             "update_id": 7, "batch": True}
    assert len(rec.sent) == 1
    text = rec.sent[0]
    assert "2 merge(s) approved" in text and "approved from Telegram" in text
    assert "N5b - N5b: inbox (tier B)" in text
    assert "D1 - D1: docs (tier A)" in text
    assert "tier C, needs its own word: merge T9" in text
    assert "tier unknown, needs its own word: merge U1" in text
    assert "excluded:" not in text and "failed," not in text


def test_merge_all_except_skips_named(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "D1: docs", "A")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 8, batch=True,
                                                   exclude=("n5b",))], rec)
    assert results[0]["applied"] is True
    assert results[0]["approved"] == [b.id]
    assert _pending(act, a.id) == 1 and _approved(act, a.id) == []
    assert _pending(act, b.id) == 0 and len(_approved(act, b.id)) == 1
    assert [e.payload["run_id"] for e in _decisions(act)] == [b.id]
    assert len(rec.sent) == 1
    assert "1 merge(s) approved" in rec.sent[0]
    assert "excluded: N5b" in rec.sent[0]
    # a candidate can also be excluded by its full run id
    c = _tiered(act, "S3a: squash", "B")
    results = apply_commands(act, [TelegramCommand("merge", None, 9, batch=True,
                                                   exclude=(c.id, "N5b"))], rec)
    assert results[0]["applied"] is False and results[0]["reason"] == "nothing applicable"
    assert _pending(act, a.id) == 1 and _pending(act, c.id) == 1
    assert "excluded: N5b, S3a" in rec.sent[-1]


def test_merge_all_with_unknown_excluded_name_approves_nothing(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "D1: docs", "A")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 8, batch=True,
                                                   exclude=("N5d",))], rec)  # typo for N5b
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "unknown excluded name"}]
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    assert _approved(act, a.id) == [] and _approved(act, b.id) == []
    assert _decisions(act) == []
    assert len(rec.sent) == 1
    assert "NOT applied" in rec.sent[0] and "N5d" in rec.sent[0]
    assert "n5d" not in rec.sent[0]
    assert "waiting: N5b, D1" in rec.sent[0]


def test_merge_all_reports_a_failing_run_and_keeps_the_rest(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    a = _tiered(act, "N1: one", "B")
    b = _tiered(act, "N2: two", "B")
    c = _tiered(act, "N3: three", "B")
    real = act.decide_approval
    state = {"raised": False}

    def flaky(approval_id, decision, actor, note=None):
        if act.approvals[approval_id].target == b.id and not state["raised"]:
            state["raised"] = True
            raise RuntimeError("ACT hiccup at http://act.local/botSECRETTOKEN")
        return real(approval_id, decision, actor) if note is None \
            else real(approval_id, decision, actor, note=note)
    act.decide_approval = flaky
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 9, batch=True)], rec)
    assert results[0]["applied"] is True
    assert sorted(results[0]["approved"]) == sorted([a.id, c.id])
    assert _pending(act, a.id) == 0 and _pending(act, c.id) == 0
    assert _pending(act, b.id) == 1 and _approved(act, b.id) == []
    ev = _decisions(act)
    assert len(ev) == 2 and sorted(e.payload["run_id"] for e in ev) == sorted([a.id, c.id])
    assert len(rec.sent) == 1
    text = rec.sent[0]
    assert "2 merge(s) approved" in text
    assert "failed, re-send 'merge <name>' for: N2 (RuntimeError)" in text
    assert "SECRETTOKEN" not in text


def test_merge_all_off_changes_nothing(monkeypatch):
    act = FakeACTClient()
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "D1: docs", "A")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "batch off"}]
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    assert _decisions(act) == []
    assert rec.sent == [BATCH_OFF]
    # a malformed value is off too
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "maybe")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 2, batch=True,
                                                   exclude=("N5b",))], rec)
    assert results[0]["applied"] is False and results[0]["reason"] == "batch off"
    assert _pending(act, a.id) == 1 and _pending(act, b.id) == 1
    assert rec.sent == [BATCH_OFF]
    # the single-run forms do not depend on the flag
    results = apply_commands(act, [TelegramCommand("merge", None, 3, name="N5b")], rec)
    assert results[0]["applied"] is True and _pending(act, a.id) == 0


def test_merge_all_with_nothing_waiting(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "nothing waiting for merge"}]
    assert act.events == []
    assert rec.sent == ["Navigator: merge all - nothing waiting for merge"]


def test_merge_all_never_touches_unaudited_runs(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    run = _run(act, "X1: x", audited=False)
    act.emit_event(Event(kind="run.awaiting_review", correlation_id=run.correlation_id,
                         ts=datetime.now(timezone.utc) - timedelta(seconds=0.5),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA,
                                  "merge_tier": "B"}))
    assert merge_tier_of(act, run) == "B" and not is_audited(act, run.id)
    _run(act, "S4: no approval", pending=False)
    _run(act, "S5: still building", phase=RunPhase.building, audited=False)
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results[0]["applied"] is False
    assert results[0]["reason"] == "nothing waiting for merge"
    assert _pending(act, run.id) == 1 and _approved(act, run.id) == []
    assert _decisions(act) == []


def test_merge_all_through_poll_once_and_bare_merge_still_lists_both(tmp_path, monkeypatch):
    # 'merge all' no longer parses as the short name 'all'; bare 'merge' is unchanged
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = FakeACTClient()
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "S3a: squash", "B")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(1, "merge"), _update(2, "merge all except S3a")]).poll_once()
    assert cmds == [TelegramCommand("merge", None, 1),
                    TelegramCommand("merge", None, 2, batch=True, exclude=("S3a",))]
    results = apply_commands(act, cmds, rec)
    assert results[0]["reason"] == "ambiguous: several runs waiting for merge"
    assert results[1]["applied"] is True and results[1]["approved"] == [a.id]
    assert _pending(act, a.id) == 0 and _pending(act, b.id) == 1
    assert "not applied" in rec.sent[0] and "excluded: S3a" in rec.sent[1]
