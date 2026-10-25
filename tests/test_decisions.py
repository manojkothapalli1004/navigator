"""Decisions inbox (navigator/observability/decisions.py): collect, the 4h nudge, the
8am digest, quiet hours, state persistence and its failure modes, the Telegram
'decisions' command and the daemon step.

All times are built as aware datetimes in America/New_York and passed as `now`.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import navigator.__main__ as main_mod
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ApprovalGate, ProposalStatus, RunPhase, RunStatus
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.observability import decisions, telegram_inbox
from navigator.observability.telegram_proposals import (HELP, handle_operator_message,
                                                        parse_proposal_command)

NY = ZoneInfo("America/New_York")
CHAT = "4242"
AUDITED: set[str] = set()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_TZ", "America/New_York")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_STATE_PATH", str(tmp_path / "decisions_state.json"))
    monkeypatch.setenv("NAVIGATOR_MANUAL_DECISIONS_PATH", str(tmp_path / "manual_decisions.json"))
    AUDITED.clear()
    # _candidates asks cli.is_audited through the telegram_inbox module global
    monkeypatch.setattr(telegram_inbox, "is_audited", lambda act, run_id: run_id in AUDITED)


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class _Raising:
    def send(self, text):
        raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage")


def _ny(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


def _proposal(act, title, created_at=None):
    extra = {"created_at": created_at} if created_at is not None else {}
    p = Proposal(origin="periodic", proposal_origin_hash=f"h-{title}", title=title,
                 rationale="r", repo="/repo", base_commit_sha="abc", intended_changes="x",
                 affected_files=["a.py"], **extra)
    return act.create_proposal(p)


def _merge_run(act, title, audited=True):
    """An open run at the merge gate (awaiting review, pending merge approval) whose
    proposal is approved (so it is not also listed as a proposal)."""
    p = _proposal(act, title)
    act.set_proposal_status(p.id, ProposalStatus.approved)
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="abc", branch=f"nav/{p.id}",
                             phase=RunPhase.awaiting_review))
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    if audited:
        AUDITED.add(run.id)
    return run


def _seed(path, first_seen, digest, nudged=None):
    assert decisions.write_state(path, {
        "first_seen": {k: v.isoformat() for k, v in first_seen.items()},
        "nudged": nudged or {}, "digest": digest})


def _msg(text, date=1_700_000_000):
    """A Telegram message from the operator in the operator's private chat."""
    return {"message_id": 1, "date": date, "text": text,
            "chat": {"id": int(CHAT), "type": "private"}, "from": {"id": int(CHAT)}}


# ---- a) collect -----------------------------------------------------------------

def test_collect_lists_merge_manual_proposal_in_order(tmp_path):
    act = FakeACTClient()
    run = _merge_run(act, "Quota: pause builds")
    _merge_run(act, "Later: not audited", audited=False)
    prop = _proposal(act, "N20: trim logs")
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install the plist", "launchctl load ~/x.plist")
    items = decisions.collect(act, manual_file=str(manual))
    assert [d.kind for d in items] == ["merge", "manual", "proposal"]
    assert items[0].key == f"merge:{run.id}" and items[0].reply == "merge Quota"
    assert items[0].title == "Quota: pause builds" and items[0].since is None
    assert items[1].key == "manual:plist"
    assert items[1].reply.startswith("do yourself: ") and "launchctl" in items[1].reply
    assert items[2].key == f"proposal:{prop.id}" and "approve N20" in items[2].reply
    assert items[2].since is not None and items[2].since.tzinfo is not None
    assert not any("not audited" in d.title for d in items)


def test_collect_orders_oldest_first_within_a_kind():
    act = FakeACTClient()
    new = _proposal(act, "New: x", created_at=_ny(24, 9))
    old = _proposal(act, "Old: x", created_at=_ny(20, 9))
    assert [d.key for d in decisions.collect(act)] == [f"proposal:{old.id}", f"proposal:{new.id}"]


# ---- b) manual file -------------------------------------------------------------

def test_manual_file_round_trip(tmp_path):
    path = tmp_path / "m.json"
    assert decisions.read_manual(path) == []
    decisions.add_manual(path, "charger", "plug the charger", "walk over")
    decisions.add_manual(path, "plist", "install plist", "launchctl load")
    decisions.add_manual(path, "charger", "plug the charger in", "walk over, plug in")
    items = decisions.read_manual(path)
    assert [it["id"] for it in items] == ["charger", "plist"]
    assert items[0]["title"] == "plug the charger in" and items[0]["added"]
    assert decisions.remove_manual(path, "charger") is True
    assert decisions.remove_manual(path, "charger") is False
    assert [it["id"] for it in decisions.read_manual(path)] == ["plist"]
    with pytest.raises(ValueError):
        decisions.add_manual(path, "bad id!", "t", "h")
    with pytest.raises(ValueError):
        decisions.add_manual(path, "-leading", "t", "h")


def test_garbage_manual_file_is_empty_and_act_items_still_listed(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    act = FakeACTClient()
    _proposal(act, "N1: x")
    assert decisions.read_manual(path) == []
    assert [d.kind for d in decisions.collect(act, manual_file=str(path))] == ["proposal"]
    path.write_text(json.dumps({"items": [{"id": "ok", "title": "t", "how": "h"},
                                          {"id": 3, "title": "t", "how": "h"}, "x"]}))
    assert [it["id"] for it in decisions.read_manual(path)] == ["ok"]
    path.write_text(json.dumps(["items"]))
    assert decisions.read_manual(path) == []


def test_long_how_is_cut(tmp_path):
    path = tmp_path / "m.json"
    decisions.add_manual(path, "long", "t", "x" * 400)
    (item,) = decisions.collect(FakeACTClient(), manual_file=str(path))
    assert item.reply == "do yourself: " + "x" * 300 + "..."


def test_manual_label_is_do_yourself_without_script_and_run_with_script():
    items = decisions._manual_decisions([
        {"id": "a", "title": "T", "how": "plug in", "added": None},
        {"id": "b", "title": "U", "how": "h", "added": None,
         "script": "/x/b.sh", "sha256": "0" * 64},
    ])
    by_key = {d.key: d for d in items}
    assert set(by_key) == {"manual:a", "manual:b"}
    assert by_key["manual:a"].reply == "do yourself: plug in"
    assert "in terminal" not in by_key["manual:a"].reply
    assert by_key["manual:a"].kind == "manual" and by_key["manual:a"].title == "T"
    assert by_key["manual:b"].reply == "run b (send 'show b' to read the script)"


# ---- c, d) nudge ----------------------------------------------------------------

def test_nudge_after_4h_once_per_day(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5)}, "2026-10-24")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert out == {"items": 1, "digest_sent": False, "nudged": 1}
    assert len(rec.sent) == 1
    assert rec.sent[0].startswith("Reminder") and "merge Quota" in rec.sent[0]
    assert "waiting 5h" in rec.sent[0]
    assert decisions.read_state(state)["nudged"] == {f"merge:{run.id}": "2026-10-24"}
    decisions.decisions_tick(act, rec, now=_ny(24, 10, 15), state_file=state)
    assert len(rec.sent) == 1
    s = decisions.read_state(state)
    s["digest"] = "2026-10-25"
    assert decisions.write_state(state, s)
    decisions.decisions_tick(act, rec, now=_ny(25, 10), state_file=state)
    assert len(rec.sent) == 2
    assert rec.sent[1].startswith("Reminder") and "merge Quota" in rec.sent[1]


def test_no_nudge_under_4h_and_never_for_proposals(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    prop = _proposal(act, "N20: old", created_at=now - timedelta(days=2))
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=3),
                  f"proposal:{prop.id}": now - timedelta(days=2)}, "2026-10-24")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state)
    assert rec.sent == [] and out["nudged"] == 0 and out["items"] == 2


# ---- e) quiet hours -------------------------------------------------------------

@pytest.mark.parametrize("hour,minute", [(23, 30), (7, 59)])
def test_quiet_hours_send_nothing_but_record_first_seen(tmp_path, hour, minute):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, hour, minute)
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5)}, None)
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install plist", "launchctl load", now=now)
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   manual_file=str(manual))
    assert rec.sent == [] and out == {"quiet": True, "items": 2}
    s = decisions.read_state(state)
    assert "manual:plist" in s["first_seen"] and f"merge:{run.id}" in s["first_seen"]
    assert s["digest"] is None


# ---- f) digest ------------------------------------------------------------------

def test_morning_digest_once_then_no_nudge(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    _proposal(act, "N20: trim logs")
    manual = tmp_path / "m.json"
    now = _ny(25, 8)
    decisions.add_manual(manual, "plist", "install plist", "launchctl load",
                         now=now - timedelta(hours=6))
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5),
                  "manual:plist": now - timedelta(hours=6)}, "2026-10-24")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   manual_file=str(manual))
    assert out == {"items": 3, "digest_sent": True, "nudged": 0}
    assert len(rec.sent) == 1
    text = rec.sent[0]
    assert text.startswith("Morning digest") and "(3)" in text
    assert "merge Quota" in text and "install plist" in text and "approve N20" in text
    assert "waiting 5h" in text and "waiting 6h" in text
    decisions.decisions_tick(act, rec, now=_ny(25, 8, 15), state_file=state,
                             manual_file=str(manual))
    assert len(rec.sent) == 1
    assert decisions.read_state(state)["digest"] == "2026-10-25"


def test_digest_with_nothing_waiting_sets_date_and_sends_nothing(tmp_path):
    rec, state = _Recorder(), tmp_path / "s.json"
    out = decisions.decisions_tick(FakeACTClient(), rec, now=_ny(25, 9), state_file=state,
                                   manual_file=str(tmp_path / "none.json"))
    assert rec.sent == [] and out == {"items": 0, "digest_sent": False, "nudged": 0}
    assert decisions.read_state(state)["digest"] == "2026-10-25"


# ---- g) a notifier that raises --------------------------------------------------

def test_raising_notifier_leaves_state_and_next_tick_retries(tmp_path):
    act, state = FakeACTClient(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5)}, "2026-10-24")
    before = decisions.read_state(state)
    with pytest.raises(RuntimeError):
        decisions.decisions_tick(act, _Raising(), now=now, state_file=state)
    assert decisions.read_state(state) == before
    rec = _Recorder()
    decisions.decisions_tick(act, rec, now=_ny(24, 10, 15), state_file=state)
    assert len(rec.sent) == 1
    assert "Reminder" in rec.sent[0] and "merge Quota" in rec.sent[0]


# ---- h) vanished items ----------------------------------------------------------

def test_vanished_items_drop_out_of_state(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install plist", "launchctl load")
    _seed(state, {}, "2026-10-24",
          nudged={f"merge:{run.id}": "2026-10-24", "manual:plist": "2026-10-24"})
    decisions.decisions_tick(act, rec, now=_ny(24, 10), state_file=state,
                             manual_file=str(manual))
    s = decisions.read_state(state)
    assert set(s["first_seen"]) == {f"merge:{run.id}", "manual:plist"}
    assert set(s["nudged"]) == {f"merge:{run.id}", "manual:plist"}
    stored = act.runs[run.id]
    stored.status = RunStatus.completed  # merged
    act.update_run(stored)
    assert decisions.remove_manual(manual, "plist")
    decisions.decisions_tick(act, rec, now=_ny(24, 10, 15), state_file=state,
                             manual_file=str(manual))
    s = decisions.read_state(state)
    assert s["first_seen"] == {} and s["nudged"] == {}
    assert rec.sent == []


# ---- i) the Telegram command ----------------------------------------------------

def test_decisions_command_sends_the_list(tmp_path):
    act, rec = FakeACTClient(), _Recorder()
    _proposal(act, "N20: trim logs")
    decisions.add_manual(decisions.manual_path(), "plist", "install plist", "launchctl load")
    msg = _msg("decisions")
    assert handle_operator_message(msg, act, rec, CHAT, now=msg["date"] + 10) is True
    assert len(rec.sent) == 1
    assert rec.sent[0].startswith("Waiting on you (2):")
    assert "install plist" in rec.sent[0] and "approve N20" in rec.sent[0]
    assert not (tmp_path / "decisions_state.json").exists()  # read-only: never written


def test_decisions_command_with_nothing_waiting():
    rec = _Recorder()
    msg = _msg("decisions")
    assert handle_operator_message(msg, FakeACTClient(), rec, CHAT, now=msg["date"] + 10) is True
    assert rec.sent == ["Nothing is waiting on you."]


def test_decisions_with_argument_is_not_a_command():
    rec = _Recorder()
    msg = _msg("decisions now")
    assert handle_operator_message(msg, FakeACTClient(), rec, CHAT, now=msg["date"] + 10) is False
    assert rec.sent == []
    assert parse_proposal_command("decisions now") is None
    assert parse_proposal_command("decisions").action == "decisions"
    assert parse_proposal_command("/Decisions").action == "decisions"
    assert "decisions - everything waiting on you" in HELP


def test_decisions_command_from_another_chat_is_ignored():
    rec = _Recorder()
    msg = _msg("decisions")
    msg["chat"]["id"] = 1
    assert handle_operator_message(msg, FakeACTClient(), rec, CHAT, now=msg["date"] + 10) is False
    assert rec.sent == []


# ---- j, n) the daemon step ------------------------------------------------------

def test_decisions_step_never_raises_on_notifier_error(monkeypatch):
    monkeypatch.setattr(decisions, "decisions_tick",
                        lambda act, notifier: notifier.send("probe"))
    nav = SimpleNamespace(act=FakeACTClient(), notifier=_Raising())
    summary = {}
    main_mod._decisions_step(nav, summary)
    assert summary["decisions_error"] == "RuntimeError"
    assert "SECRET" not in json.dumps(summary)


def test_decisions_step_reports_state_write_failure(monkeypatch):
    result = {"items": 1, "state_write_failed": True}
    monkeypatch.setattr(decisions, "decisions_tick", lambda act, notifier: result)
    nav = SimpleNamespace(act=FakeACTClient(), notifier=_Recorder())
    summary = {}
    main_mod._decisions_step(nav, summary)
    assert summary["decisions"] == result
    assert summary["decisions_error"] == "state_write_failed"


def test_decisions_step_uses_telegram_act_and_cycle_calls_it(monkeypatch):
    seen = {}

    def fake_tick(act, notifier):
        seen["act"] = act
        return {"items": 0, "digest_sent": False, "nudged": 0}

    monkeypatch.setattr(decisions, "decisions_tick", fake_tick)
    telegram_act = FakeACTClient()
    nav = SimpleNamespace(act=FakeACTClient(), telegram_act=telegram_act, notifier=_Recorder(),
                          telegram_inbox=None, triage=None, last_snapshot_chars=0,
                          periodic_tick=lambda: [], approval_poll=lambda: [],
                          merge_poll=lambda: [])
    summary = main_mod._cycle(nav, SimpleNamespace(mode="execute", driver="fake"))
    assert seen["act"] is telegram_act
    assert summary["decisions"] == {"items": 0, "digest_sent": False, "nudged": 0}
    assert "decisions_error" not in summary


# ---- k) formatting --------------------------------------------------------------

def test_format_folds_extra_proposals_and_chunks():
    now = _ny(24, 10)
    props = [decisions.Decision("proposal", f"proposal:{i}", f"P{i}: t",
                                f"approve P{i} / decline P{i}", now - timedelta(hours=i))
             for i in range(7)]
    text = "\n".join(decisions.format_decisions(props, now))
    assert text.startswith("Waiting on you (7):")
    assert "+2 more proposals - send all" in text
    assert "5. proposal: P4: t - waiting 4h - approve P4 / decline P4" in text
    assert "P5:" not in text and "P6:" not in text
    assert "1. proposal: P0: t - waiting <1h" in text
    big = [decisions.Decision("manual", f"manual:{i}", "t" * 500, "do yourself: " + "h" * 300, None)
           for i in range(200)]
    chunks = decisions.format_decisions(big, now)
    assert len(chunks) > 1 and all(len(c) <= 3900 for c in chunks)
    assert sum(c.count("manual: ") for c in chunks) == 200
    assert "- new -" in chunks[0]
    assert decisions.format_decisions([], now) == ["Nothing is waiting on you."]


# ---- l) a state file that cannot be written -------------------------------------

def test_unwritable_state_sends_nothing(tmp_path):
    (tmp_path / "blocker").write_text("not a directory")
    state = tmp_path / "blocker" / "s.json"
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install plist", "launchctl load")
    rec = _Recorder()
    for i in range(5):
        out = decisions.decisions_tick(FakeACTClient(), rec,
                                       now=_ny(24, 10) + timedelta(minutes=15 * i),
                                       state_file=state, manual_file=str(manual))
        assert out == {"items": 1, "state_write_failed": True}
    assert rec.sent == []
    assert decisions.write_state(state, {"first_seen": {}, "nudged": {}, "digest": None}) is False


# ---- m) a source that fails keeps its keys -------------------------------------

def test_source_failure_keeps_first_seen(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    seen = now - timedelta(hours=5)
    _seed(state, {f"merge:{run.id}": seen}, "2026-10-24")

    def broken(act):
        raise RuntimeError("ACT hiccup")

    items, failed = decisions.collect_with_status(act, candidates=broken)
    assert items == [] and failed == {"merge"}
    out = decisions.decisions_tick(act, rec, now=now, state_file=state, candidates=broken)
    assert rec.sent == [] and out["items"] == 0
    assert decisions.read_state(state)["first_seen"] == {f"merge:{run.id}": seen.isoformat()}
    decisions.decisions_tick(act, rec, now=now + timedelta(minutes=15), state_file=state)
    assert len(rec.sent) == 1 and "Reminder" in rec.sent[0]
    assert "merge Quota" in rec.sent[0] and "waiting 5h" in rec.sent[0]


# ---- o) an audit-check hiccup never resets a merge item's age -----------------

def test_audit_hiccup_keeps_merge_key_while_run_is_at_the_gate(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    seen = now - timedelta(hours=5)
    _seed(state, {f"merge:{run.id}": seen}, "2026-10-24")
    # cli.is_audited fails closed without raising: the run vanishes from _candidates
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   candidates=lambda act: [])
    assert rec.sent == [] and out["items"] == 0
    assert decisions.read_state(state)["first_seen"] == {f"merge:{run.id}": seen.isoformat()}
    rec2 = _Recorder()
    decisions.decisions_tick(act, rec2, now=now + timedelta(minutes=15), state_file=state)
    assert len(rec2.sent) == 1
    assert "Reminder" in rec2.sent[0] and "merge Quota" in rec2.sent[0]
    assert "waiting 5h" in rec2.sent[0]


def test_audit_hiccup_still_prunes_a_closed_run(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    _seed(state, {f"merge:{run.id}": now - timedelta(hours=5)}, "2026-10-24")
    stored = act.runs[run.id]
    stored.status = RunStatus.completed  # merged
    act.update_run(stored)
    decisions.decisions_tick(act, rec, now=now, state_file=state, candidates=lambda act: [])
    assert decisions.read_state(state)["first_seen"] == {}
    assert rec.sent == []


def test_list_open_runs_failure_keeps_merge_keys(tmp_path, monkeypatch):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    seen = now - timedelta(hours=5)
    _seed(state, {f"merge:{run.id}": seen}, "2026-10-24")

    def raiser(*args, **kwargs):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_open_runs", raiser)
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   candidates=lambda act: [])
    assert out["items"] == 0 and rec.sent == []
    assert decisions.read_state(state)["first_seen"] == {f"merge:{run.id}": seen.isoformat()}


# ---- p) the digest while a source fails ----------------------------------------

def test_digest_waits_when_every_source_fails_then_reports_the_gap(tmp_path, monkeypatch):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    _seed(state, {}, "2026-10-24")

    def broken(*args, **kwargs):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_proposals", broken)
    missing = str(tmp_path / "none.json")
    decisions.decisions_tick(act, rec, now=_ny(25, 8, 5), state_file=state,
                             manual_file=missing, candidates=broken)
    assert rec.sent == []
    assert decisions.read_state(state)["digest"] == "2026-10-24"
    out = decisions.decisions_tick(act, rec, now=_ny(25, 10, 5), state_file=state,
                                   manual_file=missing, candidates=broken)
    assert rec.sent == ["(could not list: merge, proposal)"]
    assert out == {"items": 0, "digest_sent": False, "nudged": 0}
    assert decisions.read_state(state)["digest"] == "2026-10-25"
    decisions.decisions_tick(act, rec, now=_ny(25, 10, 20), state_file=state,
                             manual_file=missing, candidates=broken)
    assert rec.sent == ["(could not list: merge, proposal)"]


def test_digest_waits_on_partial_failure_then_lists_and_names_the_gap(tmp_path, monkeypatch):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    manual = tmp_path / "m.json"
    decisions.add_manual(manual, "plist", "install", "do x")
    _seed(state, {}, "2026-10-24")

    def broken(*args, **kwargs):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_proposals", broken)
    decisions.decisions_tick(act, rec, now=_ny(25, 8, 5), state_file=state,
                             manual_file=str(manual))
    assert not any(t.startswith("Morning digest") for t in rec.sent)
    assert rec.sent == []
    assert decisions.read_state(state)["digest"] == "2026-10-24"
    out = decisions.decisions_tick(act, rec, now=_ny(25, 10, 5), state_file=state,
                                   manual_file=str(manual))
    assert out == {"items": 1, "digest_sent": True, "nudged": 0}
    assert len(rec.sent) == 2
    assert rec.sent[0].startswith("Morning digest") and "manual: install" in rec.sent[0]
    assert rec.sent[-1] == "(could not list: proposal)"
    assert decisions.read_state(state)["digest"] == "2026-10-25"


# ---- q) no reminder while the digest is held ----------------------------------

def test_no_reminder_while_digest_is_held_so_item_is_not_sent_twice(tmp_path, monkeypatch):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    _seed(state, {f"merge:{run.id}": _ny(25, 8, 5) - timedelta(hours=5)}, "2026-10-24")

    def raiser(*args, **kwargs):
        raise RuntimeError("ACT hiccup")

    monkeypatch.setattr(act, "list_proposals", raiser)
    out = decisions.decisions_tick(act, rec, now=_ny(25, 8, 5), state_file=state)
    assert rec.sent == [] and out["nudged"] == 0
    decisions.decisions_tick(act, rec, now=_ny(25, 10, 5), state_file=state)
    assert len(rec.sent) == 2
    assert rec.sent[0].startswith("Morning digest") and "merge Quota" in rec.sent[0]
    assert rec.sent[1] == "(could not list: proposal)"
    assert not any("Reminder" in t for t in rec.sent)
    decisions.decisions_tick(act, rec, now=_ny(25, 10, 20), state_file=state)
    assert len(rec.sent) == 2


def test_reminder_still_sent_when_digest_already_out_and_nothing_failed(tmp_path):
    act, rec, state = FakeACTClient(), _Recorder(), tmp_path / "s.json"
    run = _merge_run(act, "Quota: pause builds")
    _seed(state, {f"merge:{run.id}": _ny(25, 8, 5) - timedelta(hours=5)}, "2026-10-25")
    decisions.decisions_tick(act, rec, now=_ny(25, 8, 30), state_file=state)
    assert len(rec.sent) == 1
    assert rec.sent[0].startswith("Reminder") and "merge Quota" in rec.sent[0]


# ---- the module CLI -------------------------------------------------------------

def test_module_cli_add_done_list(capsys):
    assert decisions._cli(["add", "plist", "install plist", "launchctl load"]) == 0
    capsys.readouterr()
    assert decisions._cli(["list"]) == 0
    assert "1. manual: install plist" in capsys.readouterr().out
    assert decisions._cli(["done", "plist"]) == 0
    assert capsys.readouterr().out.strip() == "removed"
    assert decisions._cli(["done", "plist"]) == 1
    assert capsys.readouterr().out.strip() == "not found"
    assert decisions._cli(["add", "bad id", "t", "h"]) == 2
    assert decisions._cli(["bogus"]) == 2
    assert "usage:" in capsys.readouterr().err
