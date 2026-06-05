from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.contracts.enums import ProposalStatus, RiskTier

def test_origin_hash_stable_and_order_independent():
    h1 = compute_origin_hash("trader", "add rsi strategy", ["a.py","b.py"], "periodic", None)
    h2 = compute_origin_hash("trader", "add rsi strategy", ["b.py","a.py"], "periodic", None)
    assert h1 == h2 and len(h1) == 64

def test_origin_hash_changes_with_source_event():
    base = compute_origin_hash("trader", "x", ["a.py"], "event", None)
    withev = compute_origin_hash("trader", "x", ["a.py"], "event", "event-9")
    assert base != withev

def test_proposal_defaults():
    p = Proposal(title="t", rationale="r", repo="trader", base_commit_sha="abc123",
                 intended_changes="add file", affected_files=["a.py"],
                 risk_tier=RiskTier.low, origin="periodic",
                 proposal_origin_hash=compute_origin_hash("trader","x",["a.py"],"periodic",None))
    assert p.id.startswith("proposal-")
    assert p.status == ProposalStatus.proposed
    assert p.priority == 5
