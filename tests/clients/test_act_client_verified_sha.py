from navigator.contracts.run import Run
from navigator.contracts.enums import RunPhase
from tests.clients.test_act_client import _http_act


def test_http_run_roundtrip_keeps_verified_sha():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    created = act.create_run(r)
    assert created.verified_sha is None
    r.phase = RunPhase.awaiting_review
    r.verified_sha = "0123456789abcdef0123456789abcdef01234567"
    act.update_run(r)
    assert fake.runs[r.id]["payload"]["verified_sha"] == r.verified_sha
    got = act.list_open_runs()[0]
    assert got.verified_sha == r.verified_sha
    assert act.list_runs_for_proposal("proposal-x")[0].verified_sha == r.verified_sha


def test_http_run_without_verified_sha_in_payload_loads_as_none():
    act, fake = _http_act()
    r = Run(proposal_id="proposal-x", base_commit_sha="abc", branch="nav/run-1")
    act.create_run(r)
    fake.runs[r.id]["payload"].pop("verified_sha", None)  # row written by older navigator
    assert act.list_open_runs()[0].verified_sha is None
