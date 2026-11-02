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


def test_terminal_command_output_never_imported(tmp_path):
    # review 2026-11-01: `! cat .env` in the terminal is recorded as user text; it must not reach
    # the web history (or anything else the conductor shows)
    import json as _json
    from navigator.conductor import transcript as t
    p = tmp_path / "s.jsonl"
    rows = ["<bash-input>cat .env</bash-input>", "<bash-stdout>TOKEN=abc</bash-stdout><bash-stderr></bash-stderr>",
            "<local-command-stdout>x</local-command-stdout>", "<command-name>/model</command-name>",
            "<task-notification>agent done</task-notification>", "  <bash-stdout>SECRET</bash-stdout>",
            "what did we build?"]
    p.write_text("".join(_json.dumps({"type": "user", "message": {"content": r}}) + "\n" for r in rows))
    assert t.read_since(p, 0) == [{"kind": "user", "text": "what did we build?"}]
