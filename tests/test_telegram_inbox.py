import io
import json
import types

import httpx
import pytest

import navigator.__main__ as m
from navigator.cli import is_audited, run_cli
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.event import Event
from navigator.contracts.run import Run
from navigator.ids import new_id
from navigator.observability.notify import NotifyingACT
from navigator.observability.telegram_inbox import (TELEGRAM_ACTOR, TelegramCommand,
                                                    TelegramInbox, apply_commands,
                                                    make_telegram_inbox, parse_command)

CHAT = "4242"


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _update(uid, text, chat=CHAT):
    return {"update_id": uid,
            "message": {"message_id": uid, "date": 1760000000 + uid,
                        "chat": {"id": int(chat), "type": "private"},
                        "from": {"id": int(chat)}, "text": text}}


def _inbox(tmp_path, updates, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(dict(req.url.params))
        return httpx.Response(200, json={"ok": True, "result": updates})
    return TelegramInbox("TOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


def _inbox_lines(tmp_path):
    p = tmp_path / "inbox.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _run(act, phase=RunPhase.awaiting_review, pending=True, audited=False):
    run = act.create_run(Run(proposal_id="proposal-x", base_commit_sha="abc",
                             branch="nav/x", phase=phase))
    if pending:
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    if audited:
        assert run_cli(["audited", run.id], act=act, out=io.StringIO()) == 0
    return run


def _merge(act, run_id, decision=None):
    return act.list_approvals(target=run_id, gate=ApprovalGate.merge, decision=decision)


def _decisions(act):
    return [e for e in act.events if e.kind == "run.telegram_decision"]


def test_unauthorized_chat_is_ignored_and_not_written(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    inbox = _inbox(tmp_path, [_update(1, f"/merge {run.id}", chat="999")])
    assert inbox.poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_authorized_text_is_written_to_inbox_without_act_change(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    events_before = len(act.events)
    inbox = _inbox(tmp_path, [_update(5, "hello navigator")])
    cmds = inbox.poll_once()
    assert cmds == []
    assert _inbox_lines(tmp_path) == [{"update_id": 5, "date": 1760000005,
                                       "text": "hello navigator"}]
    assert apply_commands(act, cmds, _Recorder()) == []
    assert len(act.events) == events_before
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_merge_on_audited_run_approves_pending_merge(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(7, f"/merge {run.id}")]).poll_once()
    assert cmds == [TelegramCommand("merge", run.id, 7)]
    results = apply_commands(act, cmds, rec)
    assert results[0]["applied"] is True
    approved = _merge(act, run.id, ApprovalDecision.approved)
    assert len(approved) == 1 and approved[0].actor == TELEGRAM_ACTOR
    assert _merge(act, run.id, ApprovalDecision.pending) == []
    ev = _decisions(act)
    assert len(ev) == 1
    assert ev[0].payload == {"run_id": run.id, "decision": "approved", "update_id": 7}
    assert rec.sent == [f"Navigator: {run.id} merge approved from Telegram (applies this cycle)"]


def test_merge_on_unaudited_run_changes_nothing(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=False)
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(8, f"/merge {run.id}")]).poll_once()
    results = apply_commands(act, cmds, rec)
    assert results[0]["applied"] is False
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1
    assert _merge(act, run.id, ApprovalDecision.approved) == []
    assert _decisions(act) == []
    assert len(rec.sent) == 1 and "not audited yet" in rec.sent[0]


def test_no_pending_merge_approval_is_reported(tmp_path):
    act = FakeACTClient()
    run = _run(act, pending=False, audited=True)
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert _merge(act, run.id) == []
    assert _decisions(act) == []
    assert "no pending merge approval" in rec.sent[0]


def test_short_run_id_is_not_a_command(tmp_path):
    assert parse_command("/merge run-01M", 1) is None
    assert parse_command("/merge", 1) is None
    assert parse_command("please /merge run-01M", 1) is None
    cmds = _inbox(tmp_path, [_update(2, "/merge run-01M")]).poll_once()
    assert cmds == []
    assert _inbox_lines(tmp_path)[0]["text"] == "/merge run-01M"


def test_uppercase_command_is_accepted(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    assert parse_command(f"/MERGE {run.id}", 3) == TelegramCommand("merge", run.id, 3)
    cmds = _inbox(tmp_path, [_update(3, f"/MERGE {run.id}")]).poll_once()
    apply_commands(act, cmds, _Recorder())
    assert len(_merge(act, run.id, ApprovalDecision.approved)) == 1


def test_reject_declines_pending_merge(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(4, f"/reject {run.id}")]).poll_once()
    assert cmds == [TelegramCommand("reject", run.id, 4)]
    apply_commands(act, cmds, rec)
    declined = _merge(act, run.id, ApprovalDecision.declined)
    assert len(declined) == 1 and declined[0].actor == TELEGRAM_ACTOR
    assert _decisions(act)[0].payload["decision"] == "rejected"
    assert rec.sent == [f"Navigator: {run.id} merge rejected from Telegram (applies this cycle)"]


def test_unknown_run_is_reported_without_change(tmp_path):
    act = FakeACTClient()
    rid = new_id("run")
    rec = _Recorder()
    cmds = _inbox(tmp_path, [_update(9, f"/merge {rid}")]).poll_once()
    assert len(cmds) == 1
    results = apply_commands(act, cmds, rec)
    assert results[0]["applied"] is False
    assert act.events == [] or _decisions(act) == []
    assert "unknown or closed run" in rec.sent[0]


def test_run_not_awaiting_review_is_refused(tmp_path):
    act = FakeACTClient()
    run = _run(act, phase=RunPhase.building)
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1
    assert _decisions(act) == []
    assert "not applied" in rec.sent[0]


def test_offset_persisted_and_update_never_processed_twice(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    seen = []
    inbox = _inbox(tmp_path, [_update(10, f"/merge {run.id}")], seen)
    first = inbox.poll_once()
    assert len(first) == 1
    assert (tmp_path / "offset").read_text() == "11"
    # the server (mock) returns the same update again: it must not be reprocessed
    second = _inbox(tmp_path, [_update(10, f"/merge {run.id}")], seen).poll_once()
    assert second == []
    assert seen[0] == {"timeout": "0"}
    assert seen[1] == {"timeout": "0", "offset": "11"}
    assert len(_inbox_lines(tmp_path)) == 1


def test_poll_http_error_does_not_leak_token(tmp_path):
    def handler(req):
        return httpx.Response(500, json={"ok": False})
    inbox = TelegramInbox("SECRETTOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                          client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError) as exc:
        inbox.poll_once()
    assert "SECRETTOKEN" not in str(exc.value)
    assert not (tmp_path / "offset").exists()


class _FakeNav:
    def __init__(self, inbox, act=None):
        self.calls = []
        self.last_snapshot_chars = 0
        self.notifier = _Recorder()
        self.act = act or FakeACTClient()
        self.telegram_inbox = inbox

    def periodic_tick(self):
        return []

    def approval_poll(self):
        return []

    def merge_poll(self):
        self.calls.append("merge_poll")
        return []


_SETTINGS = types.SimpleNamespace(mode="execute", driver="fake")


def test_poll_exception_does_not_stop_merge_poll():
    class _Boom:
        def poll_once(self):
            raise RuntimeError("telegram down")
    nav = _FakeNav(_Boom())
    summary = m._cycle(nav, _SETTINGS)
    assert nav.calls == ["merge_poll"]
    assert "telegram down" in summary["telegram_error"]
    assert "error" not in summary


def test_cycle_applies_telegram_commands_before_merge_poll(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    order = []

    class _Inbox:
        def poll_once(self):
            order.append("poll")
            return [TelegramCommand("merge", run.id, 1)]

    nav = _FakeNav(_Inbox(), act)
    nav.merge_poll = lambda: order.append("merge_poll") or []
    summary = m._cycle(nav, _SETTINGS)
    assert order == ["poll", "merge_poll"]
    assert summary["telegram_commands"] == 1 and summary["telegram_applied"] == 1
    assert len(_merge(act, run.id, ApprovalDecision.approved)) == 1


def test_cycle_without_inbox_has_no_telegram_keys():
    nav = _FakeNav(None)
    summary = m._cycle(nav, _SETTINGS)
    assert not any(k.startswith("telegram") for k in summary)


def test_make_telegram_inbox_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.delenv("NAVIGATOR_TELEGRAM_COMMANDS", raising=False)
    assert make_telegram_inbox() is None
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_COMMANDS", "1")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_OFFSET_PATH", str(tmp_path / "o"))
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_INBOX_PATH", str(tmp_path / "i.jsonl"))
    inbox = make_telegram_inbox()
    assert isinstance(inbox, TelegramInbox)
    assert inbox.chat_id == CHAT
    assert inbox.offset_path == tmp_path / "o"
    assert inbox.inbox_path == tmp_path / "i.jsonl"


def test_awaiting_review_notification_ends_with_telegram_hint():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="c",
                         payload={"run_id": "run-1", "title": "Fix x", "diff": ""}))
    assert rec.sent[0].splitlines()[-1] == "telegram: /merge run-1  (after audit)"


def test_telegram_decision_event_sends_no_extra_notification():
    rec = _Recorder()
    inner = FakeACTClient()
    act = NotifyingACT(inner, rec)
    run = _run(inner, audited=True)
    apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert len(rec.sent) == 1
    assert is_audited(inner, run.id)
