"""list_events: FakeACTClient filters its recorded events; HTTPACTClient pages
GET <act>/api/v1/sources/navigator/events with limit=500 and after=<cursor>."""
from datetime import datetime, timezone

import httpx
import pytest

from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.contracts.event import Event

EVENTS_PATH = "/api/v1/sources/navigator/events"


def _event(i, kind="run.audited", ts="2026-10-15T12:00:00Z"):
    return {"id": f"event-{i:05d}", "kind": kind, "severity": "info", "ts": ts,
            "payload": {"n": i}, "correlation_id": "corr"}


def _act(handler):
    return HTTPACTClient("http://act.test:9",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pages_are_concatenated_and_the_cursor_is_passed():
    page1 = [_event(i) for i in range(500)]
    page2 = [_event(i) for i in range(500, 503)]
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        after = req.url.params.get("after")
        return httpx.Response(200, json=page1 if after is None else page2)
    events = _act(handler).list_events()
    assert len(events) == 503
    assert seen == [(EVENTS_PATH, {"limit": "500"}),
                    (EVENTS_PATH, {"limit": "500", "after": "event-00499"})]
    assert [e.payload["n"] for e in events] == list(range(503))
    first = events[0]
    assert first.kind == "run.audited" and first.correlation_id == "corr"
    assert first.id == "event-00000"


def test_dict_envelope_uses_next_cursor():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if req.url.params.get("after") is None:
            return httpx.Response(200, json={"events": [_event(i) for i in range(500)],
                                             "next_cursor": "cur-1"})
        return httpx.Response(200, json={"events": [_event(500), _event(501)]})
    events = _act(handler).list_events()
    assert len(events) == 502
    assert seen == [{"limit": "500"}, {"limit": "500", "after": "cur-1"}]


def test_kinds_filter_is_applied_client_side():
    rows = [_event(1, "run.audited"), _event(2, "run.awaiting_review"),
            _event(3, "run.building"), _event(4, "run.audited")]
    act = _act(lambda req: httpx.Response(200, json=rows))
    assert [e.payload["n"] for e in act.list_events()] == [1, 2, 3, 4]
    got = act.list_events(kinds={"run.audited", "run.awaiting_review"})
    assert [(e.kind, e.payload["n"]) for e in got] == [("run.audited", 1),
                                                      ("run.awaiting_review", 2),
                                                      ("run.audited", 4)]
    assert act.list_events(kinds={"nothing"}) == []


def test_ts_is_parsed_and_naive_means_utc():
    rows = [_event(1, ts="2026-10-15T12:00:00Z"),
            _event(2, ts="2026-10-15T12:00:00"),
            _event(3, ts="2026-10-15T14:00:00+02:00"),
            _event(4, ts="yesterday"),
            {"id": "event-5", "kind": "run.audited", "payload": {"n": 5}}]
    events = _act(lambda req: httpx.Response(200, json=rows)).list_events()
    noon = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert events[0].ts == noon and events[0].ts.tzinfo is not None
    assert events[1].ts == noon and events[1].ts.tzinfo is not None
    assert events[2].ts == noon
    assert events[3].ts is None
    assert events[4].ts is None and events[4].payload == {"n": 5}
    assert events[4].correlation_id is None


def test_more_than_fifty_pages_raises():
    calls = []

    def handler(req):
        calls.append(req.url.params.get("after"))
        base = len(calls) * 1000
        return httpx.Response(200, json=[_event(base + i) for i in range(500)])
    with pytest.raises(RuntimeError, match="pages"):
        _act(handler).list_events()
    assert len(calls) == 50


def test_http_error_raises():
    act = _act(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        act.list_events()


def test_events_url_is_outside_the_agent_resource():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=[])
    act = HTTPACTClient("http://act.test:9/",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert act.list_events() == []
    assert seen == ["http://act.test:9/api/v1/sources/navigator/events?limit=500"]


def test_fake_list_events_filters_by_kind():
    act = FakeACTClient()
    act.emit_event(Event(kind="run.audited", payload={"n": 1}))
    act.emit_event(Event(kind="run.building", payload={"n": 2}))
    act.emit_event(Event(kind="run.awaiting_review", payload={"n": 3}))
    assert [e.payload["n"] for e in act.list_events()] == [1, 2, 3]
    got = act.list_events(kinds={"run.audited", "run.awaiting_review"})
    assert [e.payload["n"] for e in got] == [1, 3]
    assert all(e.ts.tzinfo is not None for e in got)
    assert act.list_events(kinds=set()) == []
