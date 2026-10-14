from navigator.observability.notify import NotifyingACT
from navigator.clients.act_client import FakeACTClient
from navigator.contracts.event import Event


class _Recorder:
    def __init__(self):
        self.sent = []
    def send(self, text):
        self.sent.append(text)


def test_awaiting_review_shows_verified_short_sha():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="c", payload={
        "run_id": "run-1", "title": "Fix x", "diff": "",
        "verified_sha": "0123456789abcdef0123456789abcdef01234567"}))
    assert "verified 0123456789ab" in rec.sent[0]
    assert "0123456789abc" not in rec.sent[0]


def test_awaiting_review_without_verified_sha_has_no_verified_line():
    rec = _Recorder()
    act = NotifyingACT(FakeACTClient(), rec)
    act.emit_event(Event(kind="run.awaiting_review", correlation_id="c",
                         payload={"run_id": "run-1", "title": "Fix x", "diff": ""}))
    assert "verified " not in rec.sent[0]
