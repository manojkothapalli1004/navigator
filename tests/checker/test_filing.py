from navigator.checker.checks import Finding
from navigator.checker.filing import ACT_REPO, CHECKER_ORIGIN, build_proposal, file_findings
from navigator.checker.store import FindingStore
from navigator.contracts.proposal import compute_origin_hash

T0 = "2026-10-31T20:00:00+00:00"
F = Finding("/trader", "phone", "hscroll", "body", "page is 900px wide on a 412px screen", "/logs/x/phone__trader.png")


class FakeACT:
    def __init__(self, fail=False):
        self.fail, self.created = fail, []

    def create_proposal(self, p):
        if self.fail:
            raise ConnectionError("ACT down")
        self.created.append(p)
        return p


def test_proposal_fields():
    p = build_proposal(F, "abc123")
    assert p.origin == CHECKER_ORIGIN == "site-checker"
    assert p.repo == ACT_REPO
    assert p.title == "Site checker: hscroll on /trader (phone)"
    assert p.base_commit_sha == "abc123"
    assert p.proposal_origin_hash == compute_origin_hash(ACT_REPO, F.fingerprint, [], CHECKER_ORIGIN, F.fingerprint)
    for needle in ("/trader", "phone", "hscroll", "900px", "phone__trader.png", "frontend/", "vitest"):
        assert needle in p.intended_changes


def test_files_up_to_cap(tmp_path):
    store = FindingStore(tmp_path / "s.json")
    store.record([Finding(f"/p{i}", "phone", "hscroll", "body", "x") for i in range(5)], T0)
    act = FakeACT()
    ids = file_findings(store, act, "abc", cap=3)
    assert len(ids) == 3 and len(act.created) == 3
    assert len(store.open_proposals()) == 3


def test_act_down_keeps_unfiled_then_files_once(tmp_path):
    store = FindingStore(tmp_path / "s.json")
    store.record([F], T0)
    assert file_findings(store, FakeACT(fail=True), "abc") == []
    assert store.open_proposals() == {}
    act = FakeACT()
    assert len(file_findings(store, act, "abc")) == 1
    assert file_findings(store, act, "abc") == []
    assert len(act.created) == 1
