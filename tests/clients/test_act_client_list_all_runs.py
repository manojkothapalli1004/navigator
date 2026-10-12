import httpx
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.contracts.run import Run
from navigator.contracts.enums import RunStatus


def test_http_list_all_runs_requests_closed_runs_too():
    seen = {}
    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[])
    act = HTTPACTClient("http://localhost:9",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert act.list_all_runs() == []
    assert seen["path"] == "/api/v1/agent/runs"
    assert seen["params"] == {"open_only": "false", "limit": "500"}


def test_fake_list_all_runs_includes_finished_runs():
    act = FakeACTClient()
    a = act.create_run(Run(proposal_id="p", base_commit_sha="s", branch="nav/x"))
    b = act.create_run(Run(proposal_id="p", base_commit_sha="s", branch="nav/y",
                           status=RunStatus.completed))
    assert {r.id for r in act.list_all_runs()} == {a.id, b.id}
    assert [r.id for r in act.list_open_runs()] == [a.id]
