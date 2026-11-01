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
