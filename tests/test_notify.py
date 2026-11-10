import json
import httpx
from navigator.observability.notify import (NullNotifier, TelegramNotifier, NotifyingACT,
                                            load_telegram_credentials)
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.event import Event
from navigator.contracts.enums import Severity


class _Recorder:
    def __init__(self):
        self.sent = []
    def send(self, text):
        self.sent.append(text)


def test_telegram_notifier_posts_send_message():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})
    n = TelegramNotifier("TOKEN", "42", client=httpx.Client(transport=httpx.MockTransport(handler)))
    n.send("hello")
    assert seen["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert seen["body"] == {"chat_id": "42", "text": "hello"}


def test_notifier_failure_never_raises():
    # a notification is best-effort; Telegram being down must not fail a run
    def handler(req):
        raise httpx.ConnectError("down")
    n = TelegramNotifier("T", "1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    n.send("x")  # must not raise


def test_credentials_from_env_vars_then_env_file(tmp_path, monkeypatch):
    for k in ("NAVIGATOR_TELEGRAM_BOT_TOKEN", "NAVIGATOR_TELEGRAM_CHAT_ID", "NAVIGATOR_NOTIFY_ENV_FILE"):
        monkeypatch.delenv(k, raising=False)
    assert load_telegram_credentials() is None
    env = tmp_path/".env"
    env.write_text('# bot\nTELEGRAM_BOT_TOKEN="abc"\nAUTHORIZED_CHAT_ID=99\n')
    monkeypatch.setenv("NAVIGATOR_NOTIFY_ENV_FILE", str(env))
    assert load_telegram_credentials() == ("abc", "99")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_BOT_TOKEN", "direct")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_CHAT_ID", "7")
    assert load_telegram_credentials() == ("direct", "7")


def _ev(kind, severity=Severity.info, **payload):
    return Event(kind=kind, severity=severity, correlation_id="c", payload=payload)


def test_notifying_act_announces_run_events_and_dedups_merge_blocked():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.building", run_id="run-1"))
    act.emit_event(_ev("run.awaiting_review", run_id="run-1", title="Fix x",
                       diff="diff --git a/a.py b/a.py\n+x\n",
                       verification=[{"argv": "uv run pytest -q", "returncode": 0,
                                      "output_tail": "212 passed in 41.2s"}]))
    act.emit_event(_ev("run.merge_blocked", run_id="run-1", reason="dirty"))
    act.emit_event(_ev("run.merge_blocked", run_id="run-1", reason="dirty"))
    act.emit_event(_ev("run.failed", Severity.error, run_id="run-2", error="boom"))
    assert len(act.inner.events) == 5  # every event still reaches ACT
    assert len(rec.sent) == 3
    review = rec.sent[0]
    assert "run-1" in review and "Fix x" in review and "a.py" in review
    assert "python -m navigator merge run-1" in review
    assert "uv run pytest -q" in review and "212 passed" in review
    assert "dirty" in rec.sent[1]
    assert "run-2" in rec.sent[2] and "boom" in rec.sent[2]


def test_notifying_act_delegates_everything_else():
    inner = FakeACTClient()
    act = NotifyingACT(inner, NullNotifier())
    assert act.list_proposals() == []
    assert act.inner is inner


def test_verification_summary_prefers_the_test_result_line():
    from navigator.observability.notify import format_verification
    log = [{"argv": "uv run pytest -q", "returncode": 0,
            "output_tail": "16 passed in 0.80s\nCreating virtual environment\nInstalled 19 packages"}]
    assert "16 passed" in format_verification(log)


def test_verification_summary_understands_quiet_progress_only_output():
    # this repo's pytest config prints no "N passed" line, only the progress bar
    from navigator.observability.notify import format_verification
    log = [{"argv": "uv run pytest -q", "returncode": 0,
            "output_tail": "........   [100%]\nCreating virtual environment at: .venv\nInstalled 19 packages in 98ms\n"}]
    out = format_verification(log)
    assert "tests passed (100%)" in out and "Installed" not in out


def test_completed_message_carries_revert_and_auto_marker():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.completed", run_id="run-1", merge_sha="abc", auto=True,
                       revert="git -C /r revert -m 1 abc"))
    assert rec.sent[0].startswith("Navigator: run-1 merged")
    assert "automatically" in rec.sent[0]
    assert "revert: git -C /r revert -m 1 abc" in rec.sent[0]
    act.emit_event(_ev("run.completed", run_id="run-1", merge_sha="abc", auto=False,
                       revert="git -C /r revert -m 1 abc"))
    assert "automatically" not in rec.sent[1]
    assert "revert: git -C /r revert -m 1 abc" in rec.sent[1]
    act.emit_event(_ev("run.completed", run_id="run-1"))
    assert rec.sent[2] == "Navigator: run-1 merged"


def test_review_message_shows_the_tier():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.awaiting_review", run_id="run-1", title="t", diff="", verification=[],
                       verified_sha="0123456789abcdef", merge_tier="A"))
    assert "tier: A" in rec.sent[0]
    assert rec.sent[0].index("verified 0123456789ab") < rec.sent[0].index("tier: A")
    act.emit_event(_ev("run.awaiting_review", run_id="run-2", title="t", diff="", verification=[],
                       verified_sha="0123456789abcdef"))
    assert "tier:" not in rec.sent[1]
    # an unknown tier (None, or anything outside A/B/C) prints no tier line
    act.emit_event(_ev("run.awaiting_review", run_id="run-3", title="t", diff="", verification=[],
                       merge_tier=None))
    assert "tier:" not in rec.sent[2]


def test_auto_merge_line_for_checker_fix():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.completed", run_id="run-1", merge_sha="abc", auto=True, revert="git revert x",
                       origin="site-checker", title="Site checker: tap-size on / (phone)"))
    assert ("fixed site fault: Site checker: tap-size on / (phone) — merged automatically "
            "(checker re-check passed, audited)") in rec.sent[0]
    assert "tests/docs only" not in rec.sent[0]
    assert "revert: git revert x" in rec.sent[0]


# ---- B4: the Fix button on the decline message --------------------------------------

class _MarkupRecorder:
    """A notifier with the B4 signature: records (text, reply_markup) pairs."""
    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


def test_declined_message_carries_reason_and_fix_button():
    rec = _MarkupRecorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.declined", run_id="run-01ABC", reason_code="unsafe"))
    assert rec.sent == [(
        "Navigator: run-01ABC declined (reason: unsafe); worktree kept for inspection\n"
        "fix: tap Fix or reply 'fix run-01ABC'",
        {"inline_keyboard": [[{"text": "Fix", "callback_data": "fix:run-01ABC"}]]})]
    act.emit_event(_ev("run.declined", run_id="run-02DEF"))
    assert rec.sent[1][0] == ("Navigator: run-02DEF declined (reason: none given); worktree kept "
                              "for inspection\nfix: tap Fix or reply 'fix run-02DEF'")
    assert rec.sent[1][1] == {"inline_keyboard": [[{"text": "Fix", "callback_data": "fix:run-02DEF"}]]}
    act.emit_event(_ev("run.declined", run_id="run-03GHI", reason_code=None))
    assert "(reason: none given)" in rec.sent[2][0]


def test_other_messages_have_no_reply_markup():
    rec = _MarkupRecorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.awaiting_review", run_id="run-1", title="t", diff="", verification=[]))
    act.emit_event(_ev("run.failed", Severity.error, run_id="run-2", error="boom"))
    act.emit_event(_ev("run.completed", run_id="run-3"))
    act.emit_event(_ev("run.merge_blocked", run_id="run-4", reason="dirty"))
    act.emit_event(_ev("run.deferred", run_id="run-5", error="QuotaExhaustedError"))
    assert len(rec.sent) == 5
    assert all(markup is None for _, markup in rec.sent)
    assert rec.sent[0][0].startswith("Navigator: run-1 is ready for review")
    assert "/merge run-1" in rec.sent[0][0]
    assert NotifyingACT._markup("run.awaiting_review", {"run_id": "run-1"}) is None
    assert NotifyingACT._markup("run.declined", {}) is None  # no run id: no dead button


def test_one_argument_notifier_still_gets_the_declined_text():
    """Amendment 1: a notifier whose send takes only text (older fakes, other tools) gets
    the message without the markup instead of a TypeError inside merge_poll."""
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(_ev("run.declined", run_id="run-01ABC", reason_code="tests_weak"))
    assert rec.sent == ["Navigator: run-01ABC declined (reason: tests_weak); worktree kept for "
                        "inspection\nfix: tap Fix or reply 'fix run-01ABC'"]
    act.emit_event(_ev("run.failed", Severity.error, run_id="run-2", error="boom"))
    assert len(rec.sent) == 2 and "boom" in rec.sent[1]


def test_null_notifier_takes_the_declined_event_without_raising():
    act = NotifyingACT(FakeACTClient(), NullNotifier())
    act.emit_event(_ev("run.declined", run_id="run-01ABC", reason_code="unsafe"))
    assert len(act.inner.events) == 1
    NullNotifier().send("x", reply_markup={"inline_keyboard": []})
    NullNotifier().send("x")


def test_telegram_notifier_posts_reply_markup_only_when_given():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    n = TelegramNotifier("TOKEN", "42", client=httpx.Client(transport=httpx.MockTransport(handler)))
    markup = {"inline_keyboard": [[{"text": "Fix", "callback_data": "fix:run-01ABC"}]]}
    n.send("hello", reply_markup=markup)
    n.send("plain")
    n.send("none", reply_markup=None)
    assert bodies == [{"chat_id": "42", "text": "hello", "reply_markup": markup},
                      {"chat_id": "42", "text": "plain"},
                      {"chat_id": "42", "text": "none"}]
    # the decline message through the real notifier: one POST with the button
    bodies.clear()
    act = NotifyingACT(FakeACTClient(), n)
    act.emit_event(_ev("run.declined", run_id="run-01ABC", reason_code="unsafe"))
    assert len(bodies) == 1 and bodies[0]["reply_markup"] == markup
    assert bodies[0]["text"].startswith("Navigator: run-01ABC declined (reason: unsafe)")
