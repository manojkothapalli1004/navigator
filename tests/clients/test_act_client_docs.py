"""ACT documents: FakeACTClient keeps versioned bodies in memory; HTTPACTClient
reads/writes GET/PUT <act>/api/v1/agent/docs/<key>, mapping 404 -> None on read and
409 -> DocVersionConflict(current_version) on write."""
import json

import httpx
import pytest

from navigator.clients.act_client import DocVersionConflict, FakeACTClient, HTTPACTClient
from tests.support import ProtocolOnlyACT

DOC_PATH = "/api/v1/agent/docs/navigator/settings"


def _act(handler):
    return HTTPACTClient("http://act.test:9",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---- FakeACTClient ------------------------------------------------------------

def test_fake_round_trip():
    act = FakeACTClient()
    assert act.put_doc("navigator/settings", {"a": 1}, "operator") == 1
    assert act.put_doc("navigator/settings", {"a": 1}, "operator") == 2
    assert act.get_doc("navigator/settings") == ({"a": 1}, 2)


def test_fake_get_missing_is_none():
    assert FakeACTClient().get_doc("navigator/settings") is None


def test_fake_returns_copies():
    act = FakeACTClient()
    body = {"a": {"nested": 1}}
    act.put_doc("navigator/settings", body, "operator")
    body["a"]["nested"] = 99  # caller mutates its own dict after the put
    got, version = act.get_doc("navigator/settings")
    assert got == {"a": {"nested": 1}} and version == 1
    got["a"]["nested"] = 2
    got["extra"] = True
    assert act.get_doc("navigator/settings") == ({"a": {"nested": 1}}, 1)


def test_fake_conflict():
    act = FakeACTClient()
    assert act.put_doc("navigator/status", {"s": 1}, "navigator") == 1
    with pytest.raises(DocVersionConflict) as info:
        act.put_doc("navigator/status", {"s": 2}, "navigator", expected_version=0)
    assert info.value.current_version == 1
    assert isinstance(info.value, RuntimeError)
    assert "current 1" in str(info.value)
    assert act.get_doc("navigator/status") == ({"s": 1}, 1)   # stored version unchanged
    # the matching expected_version is accepted
    assert act.put_doc("navigator/status", {"s": 2}, "navigator", expected_version=1) == 2
    assert act.get_doc("navigator/status") == ({"s": 2}, 2)


def test_fake_conflict_on_missing_doc_with_nonzero_expected():
    act = FakeACTClient()
    with pytest.raises(DocVersionConflict) as info:
        act.put_doc("navigator/settings", {"a": 1}, "operator", expected_version=3)
    assert info.value.current_version == 0
    assert act.get_doc("navigator/settings") is None
    assert act.put_doc("navigator/settings", {"a": 1}, "operator", expected_version=0) == 1


# ---- HTTPACTClient -------------------------------------------------------------

def test_http_get_404_is_none():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(404, json={"detail": "not found"})
    assert _act(handler).get_doc("navigator/settings") is None
    assert seen == [("GET", DOC_PATH)]


def test_http_get_200():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={
            "key": "navigator/settings", "version": 3,
            "updated_at": "2026-10-29T12:00:00Z", "actor": "operator",
            "body": {"x": 1}})
    got = _act(handler).get_doc("navigator/settings")
    assert got == ({"x": 1}, 3)
    assert isinstance(got[1], int)
    assert seen == [("GET", DOC_PATH)]


def test_http_put_sends_body_actor_expected_version():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={
            "key": "navigator/settings", "version": 4,
            "updated_at": "2026-10-29T12:00:00Z", "actor": "operator",
            "body": {"x": 2}})
    act = _act(handler)
    assert act.put_doc("navigator/settings", {"x": 2}, "operator", expected_version=3) == 4
    assert seen["method"] == "PUT" and seen["path"] == DOC_PATH
    assert seen["json"] == {"body": {"x": 2}, "actor": "operator", "expected_version": 3}
    # without an expected_version the field is sent as null (unconditional write)
    assert act.put_doc("navigator/settings", {"x": 2}, "operator") == 4
    assert seen["json"] == {"body": {"x": 2}, "actor": "operator", "expected_version": None}


def test_http_put_409_raises_conflict():
    act = _act(lambda req: httpx.Response(
        409, json={"detail": "version_conflict", "current_version": 7}))
    with pytest.raises(DocVersionConflict) as info:
        act.put_doc("navigator/settings", {"x": 1}, "operator", expected_version=2)
    assert info.value.current_version == 7


def test_http_put_409_without_current_version_reports_zero():
    act = _act(lambda req: httpx.Response(409, json={"detail": "version_conflict"}))
    with pytest.raises(DocVersionConflict) as info:
        act.put_doc("navigator/settings", {"x": 1}, "operator", expected_version=2)
    assert info.value.current_version == 0


def test_http_get_500_raises():
    act = _act(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        act.get_doc("navigator/settings")


def test_http_put_500_raises():
    act = _act(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        act.put_doc("navigator/settings", {"x": 1}, "operator")


def test_http_doc_url_keeps_the_slash_in_the_key():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(404)
    act = HTTPACTClient("http://act.test:9/",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert act.get_doc("navigator/digest/latest") is None
    assert seen == ["http://act.test:9/api/v1/agent/docs/navigator/digest/latest"]


# ---- ProtocolOnlyACT -------------------------------------------------------------

def test_protocol_only_act_exposes_docs():
    act = ProtocolOnlyACT(FakeACTClient())
    assert act.get_doc("navigator/settings") is None
    assert act.put_doc("navigator/settings", {"a": 1}, "operator") == 1
    assert act.get_doc("navigator/settings") == ({"a": 1}, 1)
    with pytest.raises(DocVersionConflict):
        act.put_doc("navigator/settings", {"a": 2}, "operator", expected_version=0)
    assert act.put_doc("navigator/settings", {"a": 2}, "operator", expected_version=1) == 2
