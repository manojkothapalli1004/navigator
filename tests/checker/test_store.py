from navigator.checker.checks import Finding
from navigator.checker.store import FindingStore

T0 = "2026-10-31T20:00:00+00:00"
T1 = "2026-10-31T21:00:00+00:00"


def fnd(i: int) -> Finding:
    return Finding(f"/p{i}", "phone", "hscroll", "body", f"detail {i}")


def test_new_then_still_open(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    t = s.record([fnd(1)], T0)
    assert [f.page for f in t.new] == ["/p1"] and t.still_open == []
    t = s.record([fnd(1)], T1)
    assert t.new == [] and [f.page for f in t.still_open] == ["/p1"]


def test_cap_three_open(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    s.record([fnd(i) for i in range(5)], T0)
    batch = s.to_file(cap=3)
    assert len(batch) == 3
    for i, f in enumerate(batch):
        s.mark_filed(f.fingerprint, f"prop-{i}")
    assert s.to_file(cap=3) == []


def test_regression_after_merge(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    s.record([fnd(1)], T0)
    s.mark_filed(fnd(1).fingerprint, "prop-1")
    s.sync_proposals({"prop-1": "merged"})
    t = s.record([fnd(1)], T1)
    assert [f.page for f in t.regressions] == ["/p1"]
    assert t.new == [] and s.to_file() == []


def test_declined_proposal_closes_and_fault_is_not_refiled(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    s.record([fnd(1)], T0)
    s.mark_filed(fnd(1).fingerprint, "prop-1")
    s.sync_proposals({"prop-1": "declined"})
    s.record([fnd(1)], T1)
    assert s.to_file() == []


def test_cleared_when_not_seen(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    s.record([fnd(1), fnd(2)], T0)
    t = s.record([fnd(2)], T1)
    assert t.cleared == [fnd(1).fingerprint]


def test_survives_reload(tmp_path):
    p = tmp_path / "store.json"
    s = FindingStore(p)
    s.record([fnd(1)], T0)
    s.mark_filed(fnd(1).fingerprint, "prop-1")
    again = FindingStore(p)
    assert again.open_proposals() == {fnd(1).fingerprint: "prop-1"}
    assert not list(tmp_path.glob("*.tmp"))


def test_expired_or_superseded_or_aborted_close_the_fault(tmp_path):
    s = FindingStore(tmp_path / "store.json")
    s.record([fnd(1), fnd(2), fnd(3)], T0)
    for i in (1, 2, 3):
        s.mark_filed(fnd(i).fingerprint, f"prop-{i}")
    s.sync_proposals({"prop-1": "expired", "prop-2": "superseded", "prop-3": "aborted"})
    assert s.open_proposals() == {}
