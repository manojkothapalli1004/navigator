import httpx
from navigator.clients.orc_client import FakeORCClient, HTTPORCClient

def test_fake_orc_roundtrip():
    orc = FakeORCClient(memory={"decisions": []})
    assert "decisions" in orc.get_canonical_memory()
    orc.apply_memory_update(section="known_failures", note="run-1 failed: import error", replace=False)
    assert orc.updates[-1] == ("known_failures", "run-1 failed: import error", False)

def test_http_orc_uses_rest_paths():
    seen = {}
    def handler(req: httpx.Request):
        seen[req.method] = str(req.url)
        if req.url.path == "/api/memory":
            return httpx.Response(200, json={"sections": {"decisions": []}})
        return httpx.Response(200, json={"ok": True})
    transport = httpx.MockTransport(handler)
    orc = HTTPORCClient(base_url="http://localhost:9", client=httpx.Client(transport=transport))
    orc.get_canonical_memory()
    orc.apply_memory_update(section="decisions", note="x", replace=False)
    assert seen["GET"].endswith("/api/memory")
    assert seen["POST"].endswith("/api/memory/apply-update")

def test_http_orc_get_raises_on_error_status():
    import pytest
    transport = httpx.MockTransport(lambda req: httpx.Response(500, json={"error": "boom"}))
    orc = HTTPORCClient(base_url="http://localhost:9", client=httpx.Client(transport=transport))
    with pytest.raises(httpx.HTTPStatusError):
        orc.get_canonical_memory()
    with pytest.raises(httpx.HTTPStatusError):
        orc.get_digest()

def test_default_client_timeout_is_generous():
    # observed live 2026-07-15: cycle failed ReadTimeout against local ORC;
    # httpx's 5s default is too tight for a slow local service on a 15m tick
    from navigator.clients.orc_client import HTTPORCClient
    c = HTTPORCClient("http://x")
    assert c.client.timeout.read >= 30
