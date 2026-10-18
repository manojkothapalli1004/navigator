"""N11: the always-on Telegram reader. Operator 2026-10-18: Navigator should read
Telegram like chat, with no delay. run_reader long-polls getUpdates, hands every
operator message to telegram_proposals.handle_operator_message in the same iteration
and never merges or calls an LLM itself."""
import itertools
import json
import signal
import time
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

import navigator.__main__ as m
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.enums import ProposalStatus
from navigator.contracts.proposal import Proposal
from navigator.observability import telegram_reader as tr
from navigator.observability.telegram_inbox import (CONFLICT_MESSAGE, DEFAULT_INBOX_PATH,
                                                    DEFAULT_OFFSET_PATH, FETCH_FAILED_MESSAGE,
                                                    TelegramInbox)
from navigator.observability.telegram_reader import Terminated, main_reader, run_reader

CHAT = "4242"
_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _triage_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_TRIAGE_PATH", str(tmp_path / "triage.json"))


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _prop(act, title):
    n = next(_seq)
    return act.create_proposal(Proposal(
        origin="periodic", proposal_origin_hash=f"h-{n}", title=title, rationale="r",
        repo="/r", base_commit_sha="abc", intended_changes="x", affected_files=["a.py"]))


def _update(uid, text, chat=CHAT, sender=None, chat_type="private", date=None):
    return {"update_id": uid, "message": {
        "message_id": uid, "date": int(time.time()) if date is None else date,
        "chat": {"id": int(chat), "type": chat_type},
        "from": {"id": int(chat if sender is None else sender)}, "text": text}}


def _serve(tmp_path, responses, token="TOKEN", offset="0"):
    """A mock bot server answering call i with responses[i] (the last one repeats):
    a list of updates -> 200 ok, an int -> that HTTP status, an exception -> raised."""
    calls = []

    def handler(req):
        calls.append({"params": dict(req.url.params), "timeout": req.extensions.get("timeout")})
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": item})
    (tmp_path / "offset").write_text(offset)
    inbox = TelegramInbox(token, CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                          client=httpx.Client(transport=httpx.MockTransport(handler)))
    return inbox, calls


def _inbox_lines(tmp_path):
    p = tmp_path / "inbox.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


CONFLICT = f"TelegramError: {CONFLICT_MESSAGE}"
FETCH_FAILED = f"TelegramError: {FETCH_FAILED_MESSAGE}"


def _notice(kind):
    return (f"Navigator: Telegram reader not polling - {kind} "
            f"(this error is repeated at most once an hour)")


# ---- run_reader ----------------------------------------------------------------------

def test_status_is_answered_in_the_same_iteration(tmp_path):
    act = FakeACTClient()
    _prop(act, "N1: first step")
    inbox, calls = _serve(tmp_path, [[_update(1, "status")]])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, act, rec, CHAT, max_iterations=1, sleep=sleeps.append) == 0
    assert calls[0]["params"] == {"timeout": "25", "offset": "0"}
    assert calls[0]["timeout"]["read"] == 35
    assert len(rec.sent) == 1 and "N1: first step" in rec.sent[0]
    assert sleeps == []
    assert [l["text"] for l in _inbox_lines(tmp_path)] == ["status"]
    assert (tmp_path / "offset").read_text() == "2"


def test_approve_from_the_reader_changes_the_proposal(tmp_path):
    act = FakeACTClient()
    p = _prop(act, "N1: a")
    inbox, _ = _serve(tmp_path, [[_update(1, "approve 1")], []])
    rec = _Recorder()
    run_reader(inbox, act, rec, CHAT, max_iterations=2)
    assert act.get_proposal(p.id).status == ProposalStatus.approved
    assert len(rec.sent) == 1 and "approved" in rec.sent[0]


def test_plain_chat_reaches_the_inbox_but_gets_no_reply(tmp_path):
    act = FakeACTClient()
    p = _prop(act, "N1: a")
    inbox, _ = _serve(tmp_path, [[_update(1, "how is the market?")]])
    rec = _Recorder()
    run_reader(inbox, act, rec, CHAT, max_iterations=1)
    assert rec.sent == []
    assert [l["text"] for l in _inbox_lines(tmp_path)] == ["how is the market?"]
    assert act.get_proposal(p.id).status == ProposalStatus.proposed


def test_other_chat_is_ignored_entirely(tmp_path):
    act = FakeACTClient()
    p = _prop(act, "N1: a")
    inbox, _ = _serve(tmp_path, [[_update(1, "approve 1", chat="999"),
                                 _update(2, "approve 1", chat_type="group")]])
    rec = _Recorder()
    run_reader(inbox, act, rec, CHAT, max_iterations=1)
    assert rec.sent == []
    assert _inbox_lines(tmp_path) == []
    assert (tmp_path / "offset").read_text() == "3"
    assert act.get_proposal(p.id).status == ProposalStatus.proposed


def test_409_notifies_once_backs_off_and_rearms_after_success(tmp_path):
    """N11b: this test used to pin the single-success re-arm (a 409 after one good poll
    sent a second notice). Re-arming now takes 3 consecutive successful polls, so the
    409 after the lone good poll is silent; the backoff, the status reply and the
    first notice are asserted exactly as before."""
    act = FakeACTClient()
    _prop(act, "N1: a")
    inbox, calls = _serve(tmp_path, [409, 409, [_update(1, "status")], 409])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, act, rec, CHAT, max_iterations=4, sleep=sleeps.append,
                      clock=lambda: 0.0) == 0
    assert len(calls) == 4
    assert sleeps == [30.0, 30.0, 30.0]
    assert rec.sent[0] == _notice(CONFLICT)
    assert "N1: a" in rec.sent[1]
    assert len(rec.sent) == 2  # one good poll does not re-arm: the last 409 is silent


def test_alternating_409_and_success_sends_exactly_one_notice(tmp_path):
    inbox, calls = _serve(tmp_path, [409, []] * 5)
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=10,
                      sleep=sleeps.append, clock=lambda: 0.0) == 0
    assert len(calls) == 10
    assert sleeps == [30.0] * 5
    assert rec.sent == [_notice(CONFLICT)]


def test_two_consecutive_successes_do_not_rearm_the_notice(tmp_path):
    inbox, _ = _serve(tmp_path, [409, [], [], 409])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=4,
                      sleep=sleeps.append, clock=lambda: 0.0) == 0
    assert sleeps == [30.0, 30.0]
    assert rec.sent == [_notice(CONFLICT)]


def test_three_consecutive_successes_rearm_the_notice(tmp_path):
    inbox, _ = _serve(tmp_path, [409, [], [], [], 409])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=5,
                      sleep=sleeps.append, clock=lambda: 0.0) == 0
    assert sleeps == [30.0, 30.0]
    assert rec.sent == [_notice(CONFLICT), _notice(CONFLICT)]


def test_rearm_after_is_configurable(tmp_path):
    inbox, _ = _serve(tmp_path, [409, [], 409])
    rec = _Recorder()
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=3,
                      sleep=lambda s: None, clock=lambda: 0.0, rearm_after=1) == 0
    assert rec.sent == [_notice(CONFLICT), _notice(CONFLICT)]


def test_same_error_is_repeated_once_the_notice_interval_has_passed(tmp_path):
    ticks = iter([0.0, 3599.0, 3600.0])
    inbox, _ = _serve(tmp_path, [409, 409, 409])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=3,
                      sleep=sleeps.append, clock=lambda: next(ticks)) == 0
    assert sleeps == [30.0, 30.0, 30.0]
    assert rec.sent == [_notice(CONFLICT), _notice(CONFLICT)]  # 3599 s: silent; 3600 s: sent


def test_notice_interval_is_configurable(tmp_path):
    ticks = iter([0.0, 5.0, 10.0])
    inbox, _ = _serve(tmp_path, [409, 409, 409])
    rec = _Recorder()
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=3,
                      sleep=lambda s: None, clock=lambda: next(ticks),
                      notice_interval_s=10.0) == 0
    assert rec.sent == [_notice(CONFLICT), _notice(CONFLICT)]


def test_a_different_error_kind_is_announced_at_once(tmp_path):
    inbox, _ = _serve(tmp_path, [409, httpx.ConnectError("down"), 409])
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=3,
                      sleep=sleeps.append, clock=lambda: 0.0) == 0
    assert sleeps == [30.0, 30.0, 30.0]
    assert rec.sent == [_notice(CONFLICT), _notice(FETCH_FAILED), _notice(CONFLICT)]


def test_a_failing_message_does_not_stop_the_others(tmp_path, monkeypatch):
    handled = []

    def fake(msg, act, notifier, chat_id):
        if msg["text"] == "boom":
            raise RuntimeError("x")
        handled.append((msg["text"], chat_id))
        return True
    monkeypatch.setattr(tr, "handle_operator_message", fake)
    inbox, _ = _serve(tmp_path, [[_update(1, "boom"), _update(2, "status")], []])
    rec = _Recorder()
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=2) == 0
    assert handled == [("status", CHAT)]
    assert rec.sent == []


def test_token_never_reaches_logs_or_notifications(tmp_path, monkeypatch):
    act = FakeACTClient()
    _prop(act, "N1: a")
    boom = httpx.ConnectError("down: https://api.telegram.org/botSECRETTOKEN/getUpdates")
    inbox, _ = _serve(tmp_path, [boom, [_update(1, "status")]], token="SECRETTOKEN")

    def broken(msg, act, notifier, chat_id):
        raise ConnectionError("ACT down at http://act.local/botSECRETTOKEN")
    monkeypatch.setattr(tr, "handle_operator_message", broken)
    rec, sleeps = _Recorder(), []
    with capture_logs() as logs:
        assert run_reader(inbox, act, rec, CHAT, max_iterations=2, sleep=sleeps.append) == 0
    assert sleeps == [30.0]
    assert rec.sent == [_notice(FETCH_FAILED)]
    events = [(l["event"], l.get("error")) for l in logs]
    assert ("telegram_reader.poll_failed", f"TelegramError: {FETCH_FAILED_MESSAGE}") in events
    assert ("telegram_reader.handle_failed", "ConnectionError") in events
    text = json.dumps(logs, default=str) + json.dumps(rec.sent)
    assert "SECRETTOKEN" not in text and "api.telegram.org" not in text
    assert "ACT down" not in text


def test_non_telegram_poll_failure_is_named_only(tmp_path, monkeypatch):
    inbox, _ = _serve(tmp_path, [[_update(1, "status")]], token="SECRETTOKEN")

    def bad_save(offset):
        raise OSError("disk full while saving botSECRETTOKEN offset")
    monkeypatch.setattr(inbox, "_save_offset", bad_save)
    rec, sleeps = _Recorder(), []
    assert run_reader(inbox, FakeACTClient(), rec, CHAT, max_iterations=2,
                      error_backoff_s=5.0, sleep=sleeps.append) == 0
    assert sleeps == [5.0, 5.0]
    assert rec.sent == [_notice("OSError")]


# ---- main_reader / dispatch ----------------------------------------------------------

def _no_http(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("HTTP request attempted")
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(tr, "run_reader", refuse)


def test_main_reader_refuses_when_the_daemon_polls_commands(monkeypatch, capsys):
    _no_http(monkeypatch)
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_COMMANDS", "1")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_BOT_TOKEN", "SECRETTOKEN")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_CHAT_ID", CHAT)
    assert main_reader() == 2
    err = capsys.readouterr().err
    assert "NAVIGATOR_TELEGRAM_COMMANDS" in err and "SECRETTOKEN" not in err


def test_main_reader_without_credentials_exits_2(monkeypatch, capsys):
    _no_http(monkeypatch)
    monkeypatch.delenv("NAVIGATOR_TELEGRAM_COMMANDS", raising=False)
    monkeypatch.setattr(tr, "load_telegram_credentials", lambda: None)
    assert main_reader() == 2
    assert "credentials" in capsys.readouterr().err


def _creds(monkeypatch):
    monkeypatch.delenv("NAVIGATOR_TELEGRAM_COMMANDS", raising=False)
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_CHAT_ID", CHAT)


def test_main_reader_builds_from_env_and_exits_0_on_interrupt(tmp_path, monkeypatch):
    seen = {}

    def fake_run(inbox, act, notifier, chat_id, **kwargs):
        seen.update(inbox=inbox, act=act, notifier=notifier, chat_id=chat_id)
        raise KeyboardInterrupt
    monkeypatch.setattr(tr, "run_reader", fake_run)
    monkeypatch.setattr(tr, "HTTPACTClient", lambda url: ("act", url))
    _creds(monkeypatch)
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_OFFSET_PATH", str(tmp_path / "o"))
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_INBOX_PATH", str(tmp_path / "i.jsonl"))
    monkeypatch.setenv("NAVIGATOR_ACT_BASE_URL", "http://act.test:9")
    assert main_reader() == 0
    assert isinstance(seen["inbox"], TelegramInbox) and seen["chat_id"] == CHAT
    assert seen["inbox"].chat_id == CHAT
    assert seen["inbox"].offset_path == tmp_path / "o"
    assert seen["inbox"].inbox_path == tmp_path / "i.jsonl"
    assert seen["act"] == ("act", "http://act.test:9")
    assert seen["notifier"] is not None


def test_main_reader_defaults(monkeypatch):
    seen = {}

    def fake_run(inbox, act, notifier, chat_id, **kwargs):
        seen.update(inbox=inbox, act=act)
        raise KeyboardInterrupt
    monkeypatch.setattr(tr, "run_reader", fake_run)
    monkeypatch.setattr(tr, "HTTPACTClient", lambda url: ("act", url))
    _creds(monkeypatch)
    for k in ("NAVIGATOR_TELEGRAM_OFFSET_PATH", "NAVIGATOR_TELEGRAM_INBOX_PATH",
              "NAVIGATOR_ACT_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert main_reader() == 0
    assert seen["inbox"].offset_path == Path(DEFAULT_OFFSET_PATH)
    assert seen["inbox"].inbox_path == Path(DEFAULT_INBOX_PATH)
    assert seen["act"] == ("act", "http://127.0.0.1:8090")


def test_sigterm_stops_the_reader_cleanly_and_restores_the_handler(monkeypatch):
    before = signal.getsignal(signal.SIGTERM)

    def fake_run(inbox, act, notifier, chat_id, **kwargs):
        tr._on_sigterm(signal.SIGTERM, None)
    monkeypatch.setattr(tr, "run_reader", fake_run)
    monkeypatch.setattr(tr, "HTTPACTClient", lambda url: None)
    _creds(monkeypatch)
    assert main_reader() == 0
    assert signal.getsignal(signal.SIGTERM) == before
    with pytest.raises(Terminated):
        tr._on_sigterm(signal.SIGTERM, None)


def test_main_dispatches_telegram_reader_before_the_cli(monkeypatch):
    monkeypatch.setattr(tr, "main_reader", lambda: 7)
    assert m.main(["telegram-reader"]) == 7
