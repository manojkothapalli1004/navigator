"""The completion ledger (navigator/ledger.py). Every path here is under tmp_path; no network, no SDK."""
import json
import time
from pathlib import Path

import pytest

from navigator import credits, ledger

REAL = Path("/Users/Manoj/calwbot/logs/navigator/ledger.jsonl")


def lines(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines()]


def test_append_is_one_line_per_record(tmp_path):
    p = tmp_path / "l.jsonl"
    ledger.append({"v": 1, "kind": "helper", "id": "a", "title": "t"}, p)
    ledger.append({"v": 1, "kind": "build", "id": "b", "title": "u"}, p)
    assert [r["id"] for r in lines(p)] == ["a", "b"]
    assert [r["id"] for r in ledger.read(None, p)] == ["a", "b"]


def test_append_never_raises(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    ledger.append({"v": 1, "id": "a"}, f / "sub" / "l.jsonl")


def test_rotation_at_5mb(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text("x" * ledger.MAX_BYTES)
    ledger.append({"v": 1, "id": "new"}, p)
    assert (tmp_path / "l.jsonl.1").read_text() == "x" * ledger.MAX_BYTES
    assert len(lines(p)) == 1


def test_line_cap(tmp_path):
    p = tmp_path / "l.jsonl"
    rec = {"v": 1, "id": "a", "title": "t" * 500, "files": [f"/a/{'f' * 190}{i}" for i in range(50)],
           "commands": ["c" * 80] * 30}
    ledger.append(rec, p)
    raw = p.read_bytes()
    assert len(raw) <= 4001 and raw.endswith(b"\n")
    assert json.loads(raw)["id"] == "a"


def test_redacts_secrets(tmp_path, monkeypatch):
    p = tmp_path / "l.jsonl"
    monkeypatch.setattr(credits, "load_key", lambda: "KEYVALUE-zzzzzzzz-9999")
    t = ledger.Tally()
    t.step("Bash", cmd='curl -H "Authorization: Bearer sk-abcdefgh12345678" x')
    t.step("Write", path="/tmp/ghp_abcdefghijklmnopqrstuvwxyz0123456789/f.py")
    rec = t.record(id="h", title="uses KEYVALUE-zzzzzzzz-9999 here", state="done", started=1, ended=2)
    ledger.append(rec, p)
    text = p.read_text()
    for s in ("sk-abcdefgh12345678", "ghp_abcdefghijklmnopqrstuvwxyz", "KEYVALUE-zzzzzzzz-9999"):
        assert s not in text


def test_command_first_80_chars_after_redaction(tmp_path):
    secret = "sk-" + "a1B2c3D4" * 4
    cmd = "x" * 70 + " " + secret
    t = ledger.Tally()
    t.step("Bash", cmd=cmd)
    got = t.commands[0]
    assert len(got) <= 80
    assert "sk-" not in got and "a1B2" not in got


def test_count_unverified():
    assert ledger.count_unverified("UNVERIFIED: x\n- UNVERIFIED y\nnot UNVERIFIED here") == 2
    assert ledger.count_unverified(None) == 0


def test_tally_files_only_writes_and_tools_counted():
    t = ledger.Tally()
    t.step("Read", path=None)
    t.step("Edit", path="/a/b.py")
    t.step("Write", path="/a/b.py")
    t.step("Bash", cmd="pytest -q")
    t.cost(0.12)
    rec = t.record(id="h", title="T", state="done", started=1.0, ended=2.0, unverified=1)
    assert rec["tools"] == {"Read": 1, "Edit": 1, "Write": 1, "Bash": 1}
    assert rec["files"] == ["/a/b.py"] and rec["commands"] == ["pytest -q"]
    assert rec["cost_usd"] == 0.12 and rec["unverified"] == 1 and rec["v"] == 1 and rec["kind"] == "helper"
    assert ledger.Tally().record(id="h", title="T", state="done", started=1, ended=2)["cost_usd"] is None


def test_table_and_since_filter(tmp_path):
    p = tmp_path / "l.jsonl"
    now = time.time()
    ledger.append({"v": 1, "kind": "helper", "id": "old", "title": "old one", "state": "done",
                   "started": now - 5 * 86400, "ended": now - 5 * 86400 + 5, "tools": {"Read": 2}}, p)
    ledger.append({"v": 1, "kind": "helper", "id": "new", "title": "new one", "state": "done",
                   "started": now - 60, "ended": now - 10, "tools": {"Read": 2, "Bash": 1}, "cost_usd": 0.5,
                   "unverified": 3, "files": ["a"], "commands": ["b"]}, p)
    (tmp_path / "l.jsonl").open("a").write("not json\n")
    assert [r["id"] for r in ledger.read(None, p)] == ["old", "new"]
    rows = ledger.read(now - 86400, p)
    assert [r["id"] for r in rows] == ["new"]
    out = ledger.table(rows)
    assert "new" in out and "new one" in out and "old one" not in out and "0.50" in out


def test_kill_switch(tmp_path, monkeypatch):
    p = tmp_path / "l.jsonl"
    monkeypatch.setenv("CONDUCTOR_LEDGER", "0")
    ledger.append({"v": 1, "id": "a"}, p)
    assert not p.exists()
    monkeypatch.setenv("CONDUCTOR_LEDGER", "1")
    ledger.append({"v": 1, "id": "a"}, p)
    assert p.exists()


def test_canary_default_path_is_isolated(tmp_path):
    """The autouse root fixture points ledger.PATH into tmp_path: a default-path append must never reach
    the real ledger (2026-11-09: a test run spent real credits; tests must not touch live state)."""
    before = REAL.stat().st_mtime_ns if REAL.exists() else None
    assert ledger.PATH != REAL
    assert str(ledger.PATH).startswith(str(tmp_path))
    ledger.append({"v": 1, "kind": "helper", "id": "canary", "title": "canary"})  # no path: the default
    assert ledger.PATH.exists() and lines(ledger.PATH)[-1]["id"] == "canary"
    assert (REAL.stat().st_mtime_ns if REAL.exists() else None) == before
