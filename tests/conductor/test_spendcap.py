import asyncio
import hashlib
import json

import httpx
import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.conductor import builds as B
from navigator.conductor import server
from navigator.conductor.cards import MAX_SUMMARY, CardError, Cards
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store
from navigator.contracts.enums import ProposalStatus
from tests.conductor.test_server import FakeHelpers, _BCfg, _BClock, _runner

SPEC = "x" * 500


@pytest.fixture(autouse=True)
def _no_cap_env(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CARD_CAP_USD", raising=False)


def _cards(tmp_path):
    return Cards(tmp_path / "cards.json", clock=_BClock())


# ---- cards
def test_build_card_shows_cost_line(tmp_path):
    card, _ = _cards(tmp_path).raise_build_card("p1", "navigator", "T", ["a.py"], "r", est_cost_usd=2.0)
    assert "up to $2.00 per run" in card["summary"] and card["est_cost_usd"] == 2.0
    assert card["cap_usd"] == 5.0


@pytest.mark.parametrize("est", [None, 0, -1, float("nan"), float("inf"), "x"])
def test_no_cost_line_without_estimate(tmp_path, est):
    c = _cards(tmp_path)
    old, _ = c.raise_build_card("p0", "navigator", "T", ["a.py"], "r")
    card, _ = c.raise_build_card("p1", "navigator", "T", ["a.py"], "r", est_cost_usd=est)
    assert "est. cost" not in card["summary"]
    assert "est_cost_usd" not in card and "cap_usd" not in card
    assert card["summary"].replace("p1", "p0") == old["summary"]


def test_cost_line_survives_long_file_list(tmp_path):
    files = [f"some/deep/dir/{'n' * 150}{i}.py" for i in range(200)]
    card, _ = _cards(tmp_path).raise_build_card("p1", "navigator", "My title", files, "r", est_cost_usd=2.0)
    assert len(card["summary"]) <= MAX_SUMMARY
    assert card["summary"].endswith("est. cost: up to $2.00 per run (Navigator per-run cap)")
    assert "My title" in card["summary"]


def test_over_cap_card_cannot_be_approved(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_build_card("p1", "navigator", "T", ["a.py"], "r", est_cost_usd=8.0)
    assert "OVER the $5.00 card cap" in card["summary"]
    with pytest.raises(CardError, match=r"over the \$5\.00"):
        c.approve(card["id"], "web")
    assert c.get(card["id"])["state"] == "pending"
    assert c.decline(card["id"], "web")["state"] == "declined"


def test_at_cap_is_allowed(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_build_card("p1", "navigator", "T", ["a.py"], "r", est_cost_usd=5.0)
    assert c.approve(card["id"], "web")["state"] == "approved"


def test_cap_env(tmp_path, monkeypatch):
    c = _cards(tmp_path)
    monkeypatch.setenv("CONDUCTOR_CARD_CAP_USD", "10")
    card, _ = c.raise_build_card("p1", "navigator", "T", [], "r", est_cost_usd=8.0)
    assert c.over_cap(card) is None
    for bad in ("abc", "0", "-3", "nan", ""):
        monkeypatch.setenv("CONDUCTOR_CARD_CAP_USD", bad)
        assert "over the $5.00" in c.over_cap(card)


def test_card_cap_applies_now_not_at_raise(tmp_path, monkeypatch):
    c = _cards(tmp_path)
    monkeypatch.setenv("CONDUCTOR_CARD_CAP_USD", "10")
    card, _ = c.raise_build_card("p1", "navigator", "T", [], "r", est_cost_usd=8.0)
    monkeypatch.setenv("CONDUCTOR_CARD_CAP_USD", "5")
    with pytest.raises(CardError):
        c.approve(card["id"], "web")


def test_generic_card_never_capped(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", {"command": "git push origin main"}, "push", cwd="/r")
    assert c.over_cap(card) is None
    assert c.approve(card["id"], "web")["state"] == "approved"
    want = hashlib.sha256(json.dumps(["Bash", {"command": "git push origin main"}, "/r"], sort_keys=True,
                                     default=str).encode()).hexdigest()
    from navigator.conductor.cards import fingerprint
    assert fingerprint("Bash", {"command": "git push origin main"}, "/r") == want


# ---- server
def _env(tmp_path, monkeypatch, est):
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: _BCfg())
    cards = _cards(tmp_path)
    act = FakeACTClient()
    bs = B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda r: "s", est_cost_usd=est)
    eng = Engine(Store(tmp_path / "c"), _runner, builds=bs)
    bs.on_needs_approval = server.make_build_card_callback(cards, [eng])
    helpers = FakeHelpers()
    app = server.build_app(eng, cards=cards, helpers=helpers, builds=bs)
    rec = bs.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")
    return eng, cards, act, bs, helpers, app, rec["proposal_id"], cards.pending()[0]


def _run(tmp_path, monkeypatch, est, body):
    async def go():
        env = _env(tmp_path, monkeypatch, est)
        await env[0].start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env[5]), base_url="http://c") as c:
                await body(c, *env)
        finally:
            await asyncio.sleep(0.05)
            await env[0].stop()
    asyncio.run(go())


def test_decide_over_cap_build_409_and_act_untouched(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, pid, card):
        calls = []
        orig = bs.decide
        bs.decide = lambda *a: (calls.append(a), orig(*a))[1]
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 409 and "over the $5.00" in r.json()["error"]
        assert calls == [] and helpers.started == []
        assert act.proposals[pid].status == ProposalStatus.proposed
        r = await c.post(f"/cards/{card['code']}/decline", json={"via": "web"})
        assert r.status_code == 200 and r.json()["state"] == "declined"
    _run(tmp_path, monkeypatch, 8.0, body)


def test_decide_under_cap_build_ok(tmp_path, monkeypatch):
    async def body(c, eng, cards, act, bs, helpers, app, pid, card):
        r = await c.post(f"/cards/{card['code']}/approve", json={"via": "web"})
        assert r.status_code == 200 and r.json()["state"] == "approved"
        assert act.proposals[pid].status == ProposalStatus.approved
    _run(tmp_path, monkeypatch, 2.0, body)


def test_callback_passes_estimate(tmp_path):
    cards = _cards(tmp_path)
    eng = Engine(Store(tmp_path / "c"), _runner)
    cb = server.make_build_card_callback(cards, [eng])
    cb({"proposal_id": "p1", "project": "navigator", "title": "T", "files": ["a.py"],
        "approval_reason": "why", "est_cost_usd": 2.0})
    assert cards.pending()[0]["est_cost_usd"] == 2.0


# ---- builds
def test_build_record_has_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: _BCfg())
    b = B.Builds(tmp_path / "builds.json", FakeACTClient(), settings=object(), base_sha_fn=lambda r: "s",
                 est_cost_usd=2.0)
    seen = []
    b.on_needs_approval = seen.append
    rec = b.file("navigator", "Code", SPEC, ["navigator/a.py"], via="web")
    assert rec["est_cost_usd"] == 2.0 and seen[0]["est_cost_usd"] == 2.0


@pytest.mark.parametrize("est,auto", [(8.0, False), (2.0, True)])
def test_tier1_over_cap_goes_to_card(tmp_path, monkeypatch, est, auto):
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: _BCfg())
    act = FakeACTClient()
    b = B.Builds(tmp_path / "builds.json", act, settings=object(), base_sha_fn=lambda r: "s", est_cost_usd=est)
    seen = []
    b.on_needs_approval = seen.append
    rec = b.file("navigator", "Doc", SPEC, ["docs/a.md"], via="web")
    status = act.proposals[rec["proposal_id"]].status
    if auto:
        assert status == ProposalStatus.approved and rec["tier1"] and seen == []
    else:
        assert status == ProposalStatus.proposed and not rec["tier1"]
        assert "per-card cap" in rec["approval_reason"] and len(seen) == 1
