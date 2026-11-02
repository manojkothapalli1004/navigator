import json
from pathlib import Path

from navigator.conductor import transcript


def _w(path: Path, *records):
    with open(path, "a") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _asst(*texts):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": t} for t in texts]}}


def test_transcript_path_slug():
    assert transcript.transcript_path("abc", "/Users/Manoj/calwbot", Path("/h")) == \
        Path("/h/.claude/projects/-Users-Manoj-calwbot/abc.jsonl")
    assert transcript.transcript_path("abc", "/a/b.c", Path("/h")).parent.name == "-a-b-c"


def test_read_since_offset(tmp_path):
    p = tmp_path / "s.jsonl"
    _w(p, _user("old"))
    off = transcript.size(p)
    _w(p, _user("new"), _asst("answer"))
    assert transcript.read_since(p, off) == [{"kind": "user", "text": "new"},
                                             {"kind": "assistant", "text": "answer"}]


def test_import_skips_tool_meta_sidechain(tmp_path):
    p = tmp_path / "s.jsonl"
    _w(p,
       {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
       {**_user("meta"), "isMeta": True},
       {**_asst("side"), "isSidechain": True},
       {"type": "attachment"},
       {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
       {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
       _asst("real"))
    assert transcript.read_since(p, 0) == [{"kind": "user", "text": "hi"},
                                           {"kind": "assistant", "text": "real"}]


def test_assistant_records_merged(tmp_path):
    p = tmp_path / "s.jsonl"
    _w(p, _user("q"), _asst("one"),
       {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
       {"type": "user", "message": {"content": [{"type": "tool_result", "content": "r"}]}},
       _asst("two", "three"))
    assert transcript.read_since(p, 0) == [{"kind": "user", "text": "q"},
                                           {"kind": "assistant", "text": "one\n\ntwo\n\nthree"}]


def test_missing_file_and_bad_lines(tmp_path):
    missing = tmp_path / "none.jsonl"
    assert transcript.size(missing) == 0 and transcript.read_since(missing, 0) == []
    p = tmp_path / "s.jsonl"
    _w(p, "not json", _user("ok"), '{"type": "user", "message"')
    assert transcript.read_since(p, 0) == [{"kind": "user", "text": "ok"}]
