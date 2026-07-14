import json
import pytest
from navigator.contracts.outcome import Outcome
from navigator.runtime.outcome_store import OutcomeStore

def _oc(run_id="run-1", proposal_id="prop-1", status="completed"):
    return Outcome(run_id=run_id, proposal_id=proposal_id, status=status, summary="s")

def test_append_writes_one_json_line_per_outcome(tmp_path):
    store = OutcomeStore(tmp_path / "outcomes.jsonl")
    store.append(_oc(run_id="a"))
    store.append(_oc(run_id="b"))
    lines = (tmp_path / "outcomes.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "a"
    assert json.loads(lines[1])["run_id"] == "b"

def test_latest_returns_most_recent_first(tmp_path):
    store = OutcomeStore(tmp_path / "o.jsonl")
    for rid in ("a", "b", "c"):
        store.append(_oc(run_id=rid))
    assert [o.run_id for o in store.latest(2)] == ["c", "b"]

def test_for_proposal_filters_by_proposal_id(tmp_path):
    store = OutcomeStore(tmp_path / "o.jsonl")
    store.append(_oc(run_id="a", proposal_id="p1"))
    store.append(_oc(run_id="b", proposal_id="p2"))
    store.append(_oc(run_id="c", proposal_id="p1"))
    assert {o.run_id for o in store.for_proposal("p1")} == {"a", "c"}

def test_corrupt_trailing_line_is_skipped_not_raised(tmp_path):
    p = tmp_path / "o.jsonl"
    store = OutcomeStore(p)
    store.append(_oc(run_id="good"))
    with p.open("a") as f:
        f.write('{"run_id": "partial", "status":')  # crash mid-write, no newline
    result = store.latest(10)
    assert [o.run_id for o in result] == ["good"]

def test_read_empty_or_missing_file_returns_empty(tmp_path):
    assert OutcomeStore(tmp_path / "nope.jsonl").latest(5) == []

def test_append_only_never_rewrites_existing_lines(tmp_path):
    p = tmp_path / "o.jsonl"
    store = OutcomeStore(p)
    store.append(_oc(run_id="a"))
    first = p.read_text()
    store.append(_oc(run_id="b"))
    assert p.read_text().startswith(first)  # prior bytes untouched
