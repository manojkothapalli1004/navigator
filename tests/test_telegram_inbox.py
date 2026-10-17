import io
import json
import stat
import time
import types
from datetime import datetime, timedelta, timezone

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
from navigator.observability.telegram_inbox import (CONFLICT_MESSAGE, FETCH_FAILED_MESSAGE,
                                                    INBOX_MAX_BYTES, TELEGRAM_ACTOR,
                                                    TelegramCommand, TelegramError,
                                                    TelegramInbox, apply_commands,
                                                    make_telegram_inbox, parse_command)

CHAT = "4242"
EPOCH = 1760000000      # _update(uid) messages are dated EPOCH + uid
NOW = EPOCH + 600       # the injected clock: every _update is a few minutes old
VERIFIED_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _execute_mode(monkeypatch):
    # replies word the outcome by mode; most tests look at the execute wording
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _update(uid, text, chat=CHAT, sender=None, chat_type="private", date=None,
            key="message", **extra):
    msg = {"message_id": uid, "date": EPOCH + uid if date is None else date,
           "chat": {"id": int(chat), "type": chat_type},
           "from": {"id": int(chat if sender is None else sender)}, "text": text}
    msg.update(extra)
    return {"update_id": uid, key: msg}


def _inbox(tmp_path, updates, seen=None, offset=0, now=NOW, handler=None):
    """An inbox whose mock server always answers `updates`. offset=0 writes a fresh
    offset file (so the poll is not the learn-position first poll); offset=None
    leaves whatever offset file exists (or none) alone."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if offset is not None:
        (tmp_path / "offset").write_text(str(offset))

    def default_handler(req):
        if seen is not None:
            seen.append(dict(req.url.params))
        return httpx.Response(200, json={"ok": True, "result": updates})
    transport = httpx.MockTransport(handler or default_handler)
    return TelegramInbox("TOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                         client=httpx.Client(transport=transport), clock=lambda: now)


def _inbox_lines(tmp_path):
    p = tmp_path / "inbox.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _offset(tmp_path):
    return (tmp_path / "offset").read_text()


def _run(act, phase=RunPhase.awaiting_review, pending=True, audited=False):
    # a verified run whose run.awaiting_review event is already recorded: the audit
    # (is_audited) is bound to verified_sha and must come after that event
    run = act.create_run(Run(proposal_id="proposal-x", base_commit_sha="abc",
                             branch="nav/x", phase=phase, correlation_id="corr-x",
                             verified_sha=VERIFIED_SHA))
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="corr-x",
                         ts=datetime.now(timezone.utc) - timedelta(seconds=1),
                         payload={"run_id": run.id, "verified_sha": VERIFIED_SHA}))
    if pending:
        act.create_approval(target=run.id, gate=ApprovalGate.merge)
    if audited:
        assert run_cli(["audited", run.id], act=act, out=io.StringIO()) == 0
    return run


def _merge(act, run_id, decision=None):
    return act.list_approvals(target=run_id, gate=ApprovalGate.merge, decision=decision)


def _decisions(act):
    return [e for e in act.events if e.kind == "run.telegram_decision"]


# ---- authorization and inbox ---------------------------------------------------

def test_unauthorized_chat_is_ignored_and_not_written(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    inbox = _inbox(tmp_path, [_update(1, f"/merge {run.id}", chat="999")])
    assert inbox.poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert _offset(tmp_path) == "2"  # consumed, never replayed
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_authorized_text_is_written_to_inbox_without_act_change(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    events_before = len(act.events)
    inbox = _inbox(tmp_path, [_update(5, "hello navigator")])
    cmds = inbox.poll_once()
    assert cmds == []
    assert _inbox_lines(tmp_path) == [{"update_id": 5, "date": EPOCH + 5,
                                       "text": "hello navigator"}]
    assert apply_commands(act, cmds, _Recorder()) == []
    assert len(act.events) == events_before
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_group_chat_is_ignored_even_with_matching_id(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    updates = [_update(1, f"/merge {run.id}", chat_type="group"),
               _update(2, f"/merge {run.id}", chat_type="supergroup"),
               _update(3, f"/merge {run.id}", chat_type="channel")]
    assert _inbox(tmp_path, updates).poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert _offset(tmp_path) == "4"
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_from_id_mismatch_is_ignored(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    stranger = _update(1, f"/merge {run.id}", sender="999")
    nobody = _update(2, f"/merge {run.id}")
    del nobody["message"]["from"]
    assert _inbox(tmp_path, [stranger, nobody]).poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_non_message_updates_are_ignored(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    cmd = f"/merge {run.id}"
    updates = [_update(1, cmd, key="edited_message"),
               _update(2, cmd, key="channel_post"),
               {"update_id": 3, "callback_query": {"id": "q", "from": {"id": int(CHAT)},
                                                   "data": cmd}}]
    assert _inbox(tmp_path, updates).poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert _offset(tmp_path) == "4"
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


@pytest.mark.parametrize("key,value", [("forward_origin", {"type": "hidden_user"}),
                                       ("forward_from", {"id": int(CHAT)}),
                                       ("forward_date", EPOCH)])
def test_forwarded_message_is_not_a_command(tmp_path, key, value):
    act = FakeACTClient()
    run = _run(act, audited=True)
    inbox = _inbox(tmp_path, [_update(1, f"/merge {run.id}", **{key: value})])
    assert inbox.poll_once() == []
    lines = _inbox_lines(tmp_path)
    assert len(lines) == 1 and lines[0]["ignored"] == "forwarded"
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1


def test_message_older_than_one_hour_is_not_a_command(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    cmd = f"/merge {run.id}"
    updates = [_update(1, cmd, date=NOW - 3601),   # too old
               _update(2, cmd, date=NOW + 3600),   # far in the future: clock nonsense
               _update(3, cmd, date=NOW - 3600),   # exactly one hour: still fresh
               _update(4, cmd, date=None)]
    updates[3]["message"]["date"] = "yesterday"
    assert _inbox(tmp_path, updates).poll_once() == [TelegramCommand("merge", run.id, 3)]
    lines = _inbox_lines(tmp_path)
    assert [l.get("ignored") for l in lines] == ["stale", "stale", None, "no_date"]


def test_clock_is_injected(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    update = _update(1, f"/merge {run.id}")
    fresh = _inbox(tmp_path / "a", [update], now=EPOCH + 1 + 1800).poll_once()
    assert fresh == [TelegramCommand("merge", run.id, 1)]
    old = _inbox(tmp_path / "b", [update], now=EPOCH + 1 + 7200).poll_once()
    assert old == []
    default = TelegramInbox("T", CHAT, tmp_path / "c" / "offset", tmp_path / "c" / "inbox.jsonl",
                            client=httpx.Client(transport=httpx.MockTransport(
                                lambda req: httpx.Response(200, json={"ok": True, "result": []}))))
    assert default.clock is time.time


# ---- commands ------------------------------------------------------------------

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


def test_newline_inside_command_is_not_a_command(tmp_path):
    rid = new_id("run")
    assert parse_command(f"/merge\n{rid}", 1) is None
    assert parse_command(f"/merge\r\n{rid}", 1) is None
    assert parse_command(f"/merge {rid}\nplease", 1) is None
    assert parse_command(f"/merge {rid}\n", 1) is None
    assert parse_command(f"/merge {rid}", 1) == TelegramCommand("merge", rid, 1)
    cmds = _inbox(tmp_path, [_update(1, f"/merge\n{rid}")]).poll_once()
    assert cmds == []
    assert _inbox_lines(tmp_path)[0]["text"] == f"/merge\n{rid}"


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
    assert act.events == []
    assert "unknown or closed run" in rec.sent[0]


def test_run_not_awaiting_review_is_refused(tmp_path):
    act = FakeACTClient()
    run = _run(act, phase=RunPhase.building)
    rec = _Recorder()
    apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1
    assert _decisions(act) == []
    assert "not applied" in rec.sent[0]


def test_dry_run_reply_says_merges_apply_only_in_execute_mode(tmp_path, monkeypatch):
    expected = "from Telegram (recorded; merges apply only in execute mode)"
    for mode in ("dry_run", None):
        if mode is None:
            monkeypatch.delenv("NAVIGATOR_MODE", raising=False)
        else:
            monkeypatch.setenv("NAVIGATOR_MODE", mode)
        act = FakeACTClient()
        run = _run(act, audited=True)
        rec = _Recorder()
        results = apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
        assert results[0]["applied"] is True
        assert len(_merge(act, run.id, ApprovalDecision.approved)) == 1
        assert rec.sent == [f"Navigator: {run.id} merge approved {expected}"]
        assert "applies this cycle" not in rec.sent[0]


def test_apply_failure_replies_with_class_name_only(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)

    def boom(*args, **kwargs):
        raise ConnectionError("ACT down at http://act.local/botSECRETTOKEN")
    act.decide_approval = boom
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert results == [{"run_id": run.id, "action": "merge", "applied": False,
                        "reason": "error: ConnectionError"}]
    assert rec.sent == [f"Navigator: could not apply /merge {run.id}: ConnectionError - re-send it"]
    assert "SECRETTOKEN" not in rec.sent[0] and "ACT down" not in rec.sent[0]
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1
    assert _decisions(act) == []


def test_apply_failure_reply_failure_does_not_raise(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)

    def boom(*args, **kwargs):
        raise RuntimeError("x")
    act.decide_approval = boom

    class _Dead:
        def send(self, text):
            raise RuntimeError("telegram down")
    results = apply_commands(act, [TelegramCommand("merge", run.id, 1)], _Dead())
    assert results[0]["reason"] == "error: RuntimeError"


def test_event_log_failure_still_reports_approved(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)

    def boom(*args, **kwargs):
        raise RuntimeError("events unavailable")
    act.emit_event = boom
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", run.id, 1)], rec)
    assert results[0]["applied"] is True
    assert len(_merge(act, run.id, ApprovalDecision.approved)) == 1
    assert rec.sent == [f"Navigator: {run.id} merge approved from Telegram (applies this cycle) "
                        f"(event log failed)"]
    assert "errored" not in rec.sent[0] and "events unavailable" not in rec.sent[0]


# ---- offset --------------------------------------------------------------------

def test_offset_persisted_and_update_never_processed_twice(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    seen = []
    inbox = _inbox(tmp_path, [_update(10, f"/merge {run.id}")], seen)
    first = inbox.poll_once()
    assert len(first) == 1
    assert _offset(tmp_path) == "11"
    # the server (mock) returns the same update again: it must not be reprocessed
    second = _inbox(tmp_path, [_update(10, f"/merge {run.id}")], seen, offset=None).poll_once()
    assert second == []
    assert seen[0] == {"timeout": "0", "offset": "0"}
    assert seen[1] == {"timeout": "0", "offset": "11"}
    assert len(_inbox_lines(tmp_path)) == 1


def test_missing_offset_file_first_poll_processes_nothing(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    seen = []
    old = [_update(1, f"/merge {run.id}"), _update(2, "hello")]
    assert not (tmp_path / "offset").exists()
    assert _inbox(tmp_path, old, seen, offset=None).poll_once() == []
    assert seen[0] == {"timeout": "0"}
    assert _inbox_lines(tmp_path) == []
    assert _offset(tmp_path) == "3"
    # the same old updates again: still consumed, never replayed
    assert _inbox(tmp_path, old, seen, offset=None).poll_once() == []
    assert seen[1] == {"timeout": "0", "offset": "3"}
    # a new command after the position is known is processed normally
    cmds = _inbox(tmp_path, [_update(3, f"/merge {run.id}")], seen, offset=None).poll_once()
    assert cmds == [TelegramCommand("merge", run.id, 3)]
    assert len(_inbox_lines(tmp_path)) == 1
    assert len(_merge(act, run.id, ApprovalDecision.pending)) == 1  # nothing applied by polling


def test_unparsable_offset_file_first_poll_processes_nothing(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    inbox = _inbox(tmp_path, [_update(1, f"/merge {run.id}")], offset="garbage")
    assert inbox.poll_once() == []
    assert _inbox_lines(tmp_path) == []
    assert _offset(tmp_path) == "2"


def test_missing_offset_with_no_pending_updates_then_command_is_processed(tmp_path):
    act = FakeACTClient()
    run = _run(act, audited=True)
    assert _inbox(tmp_path, [], offset=None).poll_once() == []
    assert _offset(tmp_path) == "0"
    cmds = _inbox(tmp_path, [_update(5, f"/merge {run.id}")], offset=None).poll_once()
    assert cmds == [TelegramCommand("merge", run.id, 5)]
    assert _offset(tmp_path) == "6"


# ---- network errors ------------------------------------------------------------

def test_poll_http_error_does_not_leak_token(tmp_path):
    def handler(req):
        return httpx.Response(500, json={"ok": False})
    inbox = TelegramInbox("SECRETTOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                          client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelegramError) as exc:
        inbox.poll_once()
    assert isinstance(exc.value, RuntimeError)  # __main__ treats it like any poll failure
    assert str(exc.value) == "telegram getUpdates HTTP 500"
    assert "SECRETTOKEN" not in str(exc.value)
    assert not (tmp_path / "offset").exists()


@pytest.mark.parametrize("make_exc", [lambda url: ValueError(f"cannot reach {url}"),
                                      lambda url: httpx.ConnectError(f"down: {url}"),
                                      lambda url: httpx.InvalidURL(str(url))])
def test_any_network_exception_is_telegram_error_without_url(tmp_path, make_exc):
    def handler(req):
        raise make_exc(req.url)
    inbox = TelegramInbox("SECRETTOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                          client=httpx.Client(transport=httpx.MockTransport(handler)),
                          clock=lambda: NOW)
    with pytest.raises(TelegramError) as exc:
        inbox.poll_once()
    assert str(exc.value) == FETCH_FAILED_MESSAGE
    assert "SECRETTOKEN" not in str(exc.value) and "api.telegram.org" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert not (tmp_path / "offset").exists()


def test_409_conflict_names_another_consumer(tmp_path):
    def handler(req):
        return httpx.Response(409, json={"ok": False, "error_code": 409,
                                         "description": "Conflict: terminated by other getUpdates"})
    inbox = _inbox(tmp_path, [], handler=handler)
    with pytest.raises(TelegramError) as exc:
        inbox.poll_once()
    assert str(exc.value) == CONFLICT_MESSAGE == "another Telegram getUpdates consumer is active"
    assert _offset(tmp_path) == "0"  # untouched


def test_ok_false_and_bad_json_are_telegram_errors(tmp_path):
    inbox = _inbox(tmp_path, [], handler=lambda req: httpx.Response(200, json={"ok": False}))
    with pytest.raises(TelegramError, match="ok=false"):
        inbox.poll_once()
    inbox = _inbox(tmp_path, [], handler=lambda req: httpx.Response(200, text="<html>"))
    with pytest.raises(TelegramError, match="invalid JSON"):
        inbox.poll_once()


# ---- inbox file ----------------------------------------------------------------

def test_inbox_file_mode_is_0600(tmp_path):
    _inbox(tmp_path, [_update(1, "hi")]).poll_once()
    p = tmp_path / "inbox.jsonl"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # a pre-existing world-readable inbox is tightened on the next write
    p.chmod(0o644)
    _inbox(tmp_path, [_update(2, "again")], offset=None).poll_once()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert len(_inbox_lines(tmp_path)) == 2


def test_inbox_rotates_when_over_one_megabyte(tmp_path):
    inbox = _inbox(tmp_path, [_update(1, "hello")])
    p = tmp_path / "inbox.jsonl"
    p.write_text("x" * (INBOX_MAX_BYTES + 1))
    inbox.poll_once()
    assert (tmp_path / "inbox.jsonl.1").stat().st_size == INBOX_MAX_BYTES + 1
    assert _inbox_lines(tmp_path) == [{"update_id": 1, "date": EPOCH + 1, "text": "hello"}]
    # exactly 1 MB is not rotated
    (tmp_path / "inbox.jsonl.1").unlink()
    p.write_text("y" * INBOX_MAX_BYTES)
    _inbox(tmp_path, [_update(2, "more")], offset=None).poll_once()
    assert not (tmp_path / "inbox.jsonl.1").exists()
    assert p.stat().st_size > INBOX_MAX_BYTES


# ---- daemon cycle wiring -------------------------------------------------------

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
    assert inbox.clock is time.time


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
