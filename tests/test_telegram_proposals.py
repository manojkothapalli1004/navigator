"""Operator 2026-10-03: 'i will approve everything from telegram'. Proposals are listed,
approved and declined from Telegram; the bot hands every operator message to
handle_operator_message, which keeps the inbox's authorization and freshness checks."""
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ProposalStatus
from navigator.contracts.proposal import Proposal
from navigator.observability.telegram_proposals import (ProposalCommand,
                                                        handle_operator_message,
                                                        numbered_proposals,
                                                        parse_proposal_command)
from navigator.runtime import triage
from navigator.runtime.triage import Triage

CHAT = "4242"
NOW = 1_800_000_000.0
_seq = itertools.count(1)


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _prop(act, title, status=ProposalStatus.proposed, age_s=0):
    n = next(_seq)
    p = act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash=f"h-{n}", title=title, rationale="r",
        repo="/r", base_commit_sha="abc", intended_changes="x", affected_files=["a.py"],
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=n + age_s)))
    if status != ProposalStatus.proposed:
        act.set_proposal_status(p.id, status)
    return p


@pytest.fixture(autouse=True)
def _triage_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_TRIAGE_PATH", str(tmp_path / "triage.json"))


def _msg(text, chat=CHAT, sender=None, chat_type="private", date=None, **extra):
    m = {"chat": {"id": int(chat), "type": chat_type}, "from": {"id": int(sender or chat)},
         "date": int(NOW if date is None else date), "text": text}
    m.update(extra)
    return m


def _status(act, pid):
    return act.get_proposal(pid).status


# ---- parsing -------------------------------------------------------------------------

@pytest.mark.parametrize("text,action,targets", [
    ("status", "status", ()),
    ("/Status", "status", ()),
    ("help", "help", ()),
    ("more", "more", ()),
    ("all", "all", ()),
    ("approve 1", "approve", ("1",)),
    ("/approve N1 N2", "approve", ("N1", "N2")),
    ("decline 4-33", "decline", ("4-33",)),
    ("Decline 2, 5", "decline", ("2", "5")),
])
def test_parse_accepts(text, action, targets):
    assert parse_proposal_command(text) == ProposalCommand(action, targets)


@pytest.mark.parametrize("text", [
    "", "approve", "status please", "approve looks good?", "approve 1\napprove 2",
    "decline run-01M55ZJDSDNWEK12ZF35T1P082", None, "approve " + " ".join(["1"] * 65),
])
def test_parse_rejects(text):
    assert parse_proposal_command(text) is None


# ---- status --------------------------------------------------------------------------

def test_status_shows_top3_in_triage_order_and_more_pages():
    act = FakeACTClient()
    plain = _prop(act, "Add device-token endpoint")
    _prop(act, "old thing", status=ProposalStatus.declined)
    n = [_prop(act, f"N{i}: step {i}") for i in range(1, 5)]
    # no ranking yet: milestones first, newest first
    assert [p.id for p in numbered_proposals(act)] == [n[3].id, n[2].id, n[1].id, n[0].id,
                                                       plain.id]
    rec = _Recorder()
    assert handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "top 1-3 of 5" in rec.sent[-1] and "1. [r] N4: step 4" in rec.sent[-1]
    assert "old thing" not in rec.sent[-1] and "4." not in rec.sent[-1]
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    assert "top 4-5 of 5" in rec.sent[-1] and "5. [r] Add device-token" in rec.sent[-1]
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "top 1-3" in rec.sent[-1]


def test_numbers_follow_the_saved_ranking(tmp_path):
    act = FakeACTClient()
    a, b = _prop(act, "Add x"), _prop(act, "Add y")
    (tmp_path / "triage.json").write_text(json.dumps(
        {"ranked": [{"id": a.id, "title": a.title, "why": "first"}]}))
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "1. [r] Add x" in rec.sent[-1] and "why: first" in rec.sent[-1]
    handle_operator_message(_msg("approve 2"), act, rec, CHAT, now=NOW)
    assert _status(act, b.id) == ProposalStatus.approved
    assert _status(act, a.id) == ProposalStatus.proposed


def test_all_splits_long_listings_under_telegram_limit():
    act = FakeACTClient()
    for i in range(80):
        _prop(act, f"Idea {i}: " + "x" * 120)
    rec = _Recorder()
    handle_operator_message(_msg("all"), act, rec, CHAT, now=NOW)
    assert len(rec.sent) > 1 and all(len(t) <= 4000 for t in rec.sent)
    assert "80. Idea 0" in "\n".join(rec.sent)


# ---- approve / decline ---------------------------------------------------------------

def test_approve_by_number_and_short_name():
    act = FakeACTClient()
    a, b, c = _prop(act, "N1: a"), _prop(act, "N2: b"), _prop(act, "N3: c")
    rec = _Recorder()
    handle_operator_message(_msg("all"), act, rec, CHAT, now=NOW)
    # the list shown: N3, N2, N1 -> '1' is N3
    handle_operator_message(_msg("approve 1 n1"), act, rec, CHAT, now=NOW)
    assert _status(act, a.id) == ProposalStatus.approved
    assert _status(act, b.id) == ProposalStatus.proposed
    assert _status(act, c.id) == ProposalStatus.approved
    assert "approved 2" in rec.sent[-1] and "N1: a" in rec.sent[-1]


def test_decline_range_resolves_against_one_snapshot():
    act = FakeACTClient()
    props = [_prop(act, f"T{i}: t") for i in range(5)][::-1]  # newest first
    handle_operator_message(_msg("all"), act, _Recorder(), CHAT, now=NOW)
    handle_operator_message(_msg("decline 2-4"), act, _Recorder(), CHAT, now=NOW)
    assert [_status(act, p.id) for p in props] == [
        ProposalStatus.proposed, ProposalStatus.declined, ProposalStatus.declined,
        ProposalStatus.declined, ProposalStatus.proposed]


def test_full_proposal_id_works():
    act = FakeACTClient()
    a = _prop(act, "N1: a")
    handle_operator_message(_msg(f"approve {a.id}"), act, _Recorder(), CHAT, now=NOW)
    assert _status(act, a.id) == ProposalStatus.approved


def test_nothing_changes_when_any_target_is_bad():
    act = FakeACTClient()
    a, _ = _prop(act, "Add tests: x"), _prop(act, "Add tests: y")
    rec = _Recorder()
    handle_operator_message(_msg("all"), act, rec, CHAT, now=NOW)
    for text in ("approve 1 9", "approve 1 Add", "approve 3-1", "approve 0"):
        handle_operator_message(_msg(text), act, rec, CHAT, now=NOW)
        assert _status(act, a.id) == ProposalStatus.proposed
        assert "nothing changed" in rec.sent[-1]


def test_unknown_or_already_decided_id_is_refused():
    act = FakeACTClient()
    done = _prop(act, "N1: a", status=ProposalStatus.approved)
    rec = _Recorder()
    handle_operator_message(_msg(f"decline {done.id}"), act, rec, CHAT, now=NOW)
    assert _status(act, done.id) == ProposalStatus.approved
    assert "nothing changed" in rec.sent[-1]


# ---- authorization / freshness -------------------------------------------------------

@pytest.mark.parametrize("msg", [
    _msg("approve 1", chat="999", sender="999"),
    _msg("approve 1", sender="999"),
    _msg("approve 1", chat_type="group"),
    _msg("approve 1", date=NOW - 7200),
    _msg("approve 1", forward_date=int(NOW)),
])
def test_unauthorized_stale_or_forwarded_changes_nothing(msg):
    act = FakeACTClient()
    a = _prop(act, "N1: a")
    rec = _Recorder()
    handled = handle_operator_message(msg, act, rec, CHAT, now=NOW)
    assert _status(act, a.id) == ProposalStatus.proposed
    if msg["chat"]["id"] != int(CHAT) or msg["from"]["id"] != int(CHAT) \
            or msg["chat"]["type"] != "private":
        assert handled is False and rec.sent == []


def test_ordinary_chat_is_not_handled():
    act = FakeACTClient()
    rec = _Recorder()
    assert handle_operator_message(_msg("how is the market?"), act, rec, CHAT,
                                   now=NOW) is False
    assert rec.sent == []


def test_merge_commands_go_through_the_inbox_checks():
    act = FakeACTClient()
    rec = _Recorder()
    assert handle_operator_message(_msg("merge"), act, rec, CHAT, now=NOW) is True
    assert "nothing waiting for merge" in rec.sent[-1]


def test_act_failure_is_reported_by_class_name_only():
    class Broken(FakeACTClient):
        def list_proposals(self, status=None):
            raise ConnectionError("http://secret-token@host")
    rec = _Recorder()
    assert handle_operator_message(_msg("status"), Broken(), rec, CHAT, now=NOW) is True
    assert "ConnectionError" in rec.sent[-1] and "secret" not in rec.sent[-1]


# ---- N13: numbers bind to the list the operator saw, never to the live order ---------

def _run_triage(act, rec, tmp_path):
    return Triage(act, query_fn=None, notifier=rec, path=tmp_path / "triage.json").run()


def test_numbers_bind_to_the_list_the_operator_saw():
    act = FakeACTClient()
    a, b, c = _prop(act, "Add a"), _prop(act, "Add b"), _prop(act, "Add c")
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "1. [r] Add c" in rec.sent[-1] and "3. [r] Add a" in rec.sent[-1]
    newer = _prop(act, "N9: newer")  # a milestone: live #1 from now on
    assert numbered_proposals(act)[0].id == newer.id
    handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW)
    assert _status(act, c.id) == ProposalStatus.approved
    assert _status(act, newer.id) == ProposalStatus.proposed
    assert _status(act, a.id) == _status(act, b.id) == ProposalStatus.proposed
    assert "Add c" in rec.sent[-1] and "N9" not in rec.sent[-1]


def test_more_pages_the_same_snapshot():
    act = FakeACTClient()
    for i in range(5):
        _prop(act, f"Add {i}")
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "top 1-3 of 5" in rec.sent[-1]
    _prop(act, "N9: newer")
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    assert "top 4-5 of 5" in rec.sent[-1] and "N9" not in rec.sent[-1]
    assert "4. [r] Add 1" in rec.sent[-1] and "5. [r] Add 0" in rec.sent[-1]
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "top 1-3 of 6" in rec.sent[-1] and "1. [r] N9: newer" in rec.sent[-1]


def test_number_of_a_decided_proposal_is_refused():
    act = FakeACTClient()
    a, b, c = _prop(act, "Add a"), _prop(act, "Add b"), _prop(act, "Add c")
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    act.set_proposal_status(b.id, ProposalStatus.approved)  # #2, decided out of band
    handle_operator_message(_msg("approve 1 2"), act, rec, CHAT, now=NOW)
    assert "no longer waiting" in rec.sent[-1] and "nothing changed" in rec.sent[-1]
    assert "2. Add b" in rec.sent[-1]
    assert _status(act, c.id) == ProposalStatus.proposed
    assert _status(act, a.id) == ProposalStatus.proposed


def test_numbers_refused_without_a_list():
    act = FakeACTClient()
    a = _prop(act, "N1: a")
    rec = _Recorder()
    assert handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW) is True
    assert "send 'status'" in rec.sent[-1] and "nothing changed" in rec.sent[-1]
    assert _status(act, a.id) == ProposalStatus.proposed
    handle_operator_message(_msg("approve N1"), act, rec, CHAT, now=NOW)
    assert _status(act, a.id) == ProposalStatus.approved


def test_daemon_top3_push_is_the_list_numbers_refer_to(tmp_path):
    act = FakeACTClient()
    a, b, c = _prop(act, "Add a"), _prop(act, "Add b"), _prop(act, "Add c")
    rec = _Recorder()
    _run_triage(act, rec, tmp_path)
    assert "top 1-3 of 3" in rec.sent[-1] and "1. [r] Add c" in rec.sent[-1]
    newer = _prop(act, "N9: newer")
    handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW)
    assert _status(act, c.id) == ProposalStatus.approved
    assert _status(act, newer.id) == ProposalStatus.proposed
    assert _status(act, a.id) == _status(act, b.id) == ProposalStatus.proposed
    assert "Add c" in rec.sent[-1]


def test_all_takes_a_fresh_snapshot():
    act = FakeACTClient()
    c = _prop(act, "Add c")
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    newer = _prop(act, "N9: newer")
    handle_operator_message(_msg("all"), act, rec, CHAT, now=NOW)
    assert "1. N9: newer" in rec.sent[-1] and "2. Add c" in rec.sent[-1]
    handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW)
    assert _status(act, newer.id) == ProposalStatus.approved
    assert _status(act, c.id) == ProposalStatus.proposed


def test_more_renders_decided_entries():
    act = FakeACTClient()
    props = [_prop(act, f"Add {i}") for i in range(5)]
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    act.set_proposal_status(props[1].id, ProposalStatus.approved)  # #4 of the snapshot
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    lines = rec.sent[-1].splitlines()
    assert next(l for l in lines if l.startswith("4.")) == "4. [r] Add 1 (decided)"
    assert next(l for l in lines if l.startswith("5.")) == "5. [r] Add 0"


def test_more_after_a_push_restarts_from_the_new_list(tmp_path):
    act = FakeACTClient()
    for i in range(8):
        _prop(act, f"Add {i}")
    rec = _Recorder()
    for text in ("status", "more", "more"):
        handle_operator_message(_msg(text), act, rec, CHAT, now=NOW)
    assert "top 7-8 of 8" in rec.sent[-1]
    assert triage.load(tmp_path / "triage.json")["cursor"] == 6
    _prop(act, "N9: newer")  # the open set changes: the daemon pushes a new top 3
    _run_triage(act, rec, tmp_path)
    assert "top 1-3 of 9" in rec.sent[-1] and "1. [r] N9: newer" in rec.sent[-1]
    assert triage.load(tmp_path / "triage.json")["cursor"] == 6  # the push never pages
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    assert "top 4-6 of 9" in rec.sent[-1] and "4. [r] Add 5" in rec.sent[-1]
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    assert "top 7-9 of 9" in rec.sent[-1]


def test_range_with_a_decided_entry_is_refused():
    act = FakeACTClient()
    a, b, c = _prop(act, "Add a"), _prop(act, "Add b"), _prop(act, "Add c")
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    act.set_proposal_status(b.id, ProposalStatus.approved)
    handle_operator_message(_msg("decline 1-3"), act, rec, CHAT, now=NOW)
    assert "no longer waiting" in rec.sent[-1] and "nothing changed" in rec.sent[-1]
    assert _status(act, a.id) == ProposalStatus.proposed
    assert _status(act, c.id) == ProposalStatus.proposed


def test_malformed_shown_is_ignored(tmp_path):
    act = FakeACTClient()
    a = _prop(act, "N1: a")
    (tmp_path / "triage.json").write_text(json.dumps({"shown": {"entries": [1, 2]}}))
    rec = _Recorder()
    assert handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW) is True
    assert "send 'status'" in rec.sent[-1] and "nothing changed" in rec.sent[-1]
    assert _status(act, a.id) == ProposalStatus.proposed
    assert handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW) is True
    assert "send 'status'" in rec.sent[-1]
    assert not any("could not apply" in t for t in rec.sent)


def test_empty_list_then_more_and_numbers_say_nothing_waiting():
    act = FakeACTClient()
    rec = _Recorder()
    handle_operator_message(_msg("status"), act, rec, CHAT, now=NOW)
    assert "nothing waiting for a decision" in rec.sent[-1]
    handle_operator_message(_msg("more"), act, rec, CHAT, now=NOW)
    assert "nothing waiting for a decision" in rec.sent[-1]
    handle_operator_message(_msg("approve 1"), act, rec, CHAT, now=NOW)
    assert "nothing waiting for a decision" in rec.sent[-1]
    assert "nothing changed" in rec.sent[-1] and "no list" not in rec.sent[-1]
