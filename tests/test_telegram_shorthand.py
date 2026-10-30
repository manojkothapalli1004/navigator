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
