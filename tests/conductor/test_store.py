import json
import stat

from navigator.conductor.store import Store


def test_session_id_roundtrip_atomic(tmp_path):
    s = Store(tmp_path / "c")
    assert s.session_id is None
    s.set_session_id("sess-1")
    assert Store(tmp_path / "c").session_id == "sess-1"
    assert not list((tmp_path / "c").glob("*.tmp"))
    s.set_session_id(None)
    assert Store(tmp_path / "c").session_id is None


def test_append_assigns_id_and_ts(tmp_path):
    s = Store(tmp_path / "c")
    e = s.append({"kind": "user", "text": "hi", "source": "web"})
    assert e["id"].startswith("msg") and e["ts"].endswith("+00:00")
    assert s.append({"id": "keep", "kind": "note", "text": "x"})["id"] == "keep"


def test_history_after_and_limit(tmp_path):
    s = Store(tmp_path / "c")
    ids = [s.append({"kind": "note", "text": str(i)})["id"] for i in range(5)]
    assert [e["text"] for e in s.history()] == ["0", "1", "2", "3", "4"]
    assert [e["text"] for e in s.history(after=ids[2])] == ["3", "4"]
    assert [e["text"] for e in s.history(limit=2)] == ["3", "4"]


def test_root_is_0700(tmp_path):
    s = Store(tmp_path / "c")
    assert stat.S_IMODE((tmp_path / "c").stat().st_mode) == 0o700


def test_corrupt_history_line_is_skipped(tmp_path):
    s = Store(tmp_path / "c")
    s.append({"kind": "note", "text": "ok"})
    with open(tmp_path / "c" / "history.jsonl", "a") as fh:
        fh.write('{"kind": "note", "te')
    assert [e["text"] for e in Store(tmp_path / "c").history()] == ["ok"]
