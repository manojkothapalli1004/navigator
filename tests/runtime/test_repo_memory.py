"""Reviewer memory B1: the per-repo memory store (navigator/runtime/repo_memory.py).

One JSON line per entry under <root>/<repo basename>.jsonl, appended with mode 0600 like
credits._event. remember() never raises; the switch is off by default; root_for() resolves
settings -> env -> DEFAULT_ROOT at call time (tests/conftest.py points all three under tmp)."""
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from structlog.testing import capture_logs

from navigator.runtime import repo_memory
from navigator.runtime.repo_memory import KINDS, enabled, path_for, recent, remember, root_for

LIVE_ROOT = "/Users/Manoj/calwbot/logs/navigator/repo_memory"
SCHEMA = {"ts", "kind", "repo", "run_id", "proposal_id", "reason_code", "text", "files", "source",
          "verified_sha", "base_sha", "title"}


def _settings(tmp_path, on=True):
    return SimpleNamespace(repo_memory=on, repo_memory_root=str(tmp_path / "mem"))


def _events(logs, name):
    return [l for l in logs if l["event"] == name]


def test_remember_appends_one_schema_line_mode_0600(tmp_path):
    s = _settings(tmp_path)
    ok = remember(s, "/repos/navigator", kind="failure", run_id="run-1", proposal_id="prop-1",
                  text="verification failed | FAILED tests/t.py::test_x", files=["b.py", "a.py", "a.py"],
                  source="executor", verified_sha="v" * 40, base_sha="b" * 40, title="Add x")
    assert ok is True
    path = tmp_path / "mem" / "navigator.jsonl"
    assert path_for("/repos/navigator", root_for(s)) == path
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "mem").stat().st_mode) == 0o700
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert set(e) == SCHEMA
    assert e["kind"] == "failure"
    assert e["repo"] == "/repos/navigator"          # full path: the file name is only the basename
    assert e["run_id"] == "run-1" and e["proposal_id"] == "prop-1"
    assert e["reason_code"] is None
    assert e["text"] == "verification failed | FAILED tests/t.py::test_x"
    assert e["files"] == ["a.py", "b.py"]            # deduplicated, sorted
    assert e["source"] == "executor"
    assert e["verified_sha"] == "v" * 40 and e["base_sha"] == "b" * 40 and e["title"] == "Add x"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", e["ts"]), e["ts"]
    assert datetime.fromisoformat(e["ts"]).tzinfo is not None
    assert (datetime.now(timezone.utc) - datetime.fromisoformat(e["ts"])).total_seconds() < 60
    # compact: one line, no embedded newline even when the text carries one
    assert remember(s, "/repos/navigator", kind="audit", run_id="run-2", proposal_id="prop-1",
                    text="line one\nline two", source="auditor") is True
    lines2 = path.read_text(encoding="utf-8").splitlines()
    assert len(lines2) == 2 and lines2[0] == lines[0]   # append-only: the first line is untouched
    e2 = json.loads(lines2[1])
    assert e2["text"] == "line one\nline two" and e2["title"] is None and e2["files"] == []
    assert e2["verified_sha"] is None and e2["base_sha"] is None and e2["reason_code"] is None


def test_recent_filters_by_files_newest_first(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    root = root_for(s)
    remember(s, "/r/nav", kind="decline", run_id="r1", proposal_id="p", files=["a.py"], text="1",
             reason_code="unsafe", source="operator")
    remember(s, "/r/nav", kind="failure", run_id="r2", proposal_id="p", files=["b.py"], text="2",
             source="executor")
    remember(s, "/r/nav", kind="audit", run_id="r3", proposal_id="p", files=["a.py", "c.py"], text="3",
             source="auditor")
    assert [e["run_id"] for e in recent("/r/nav", root=root)] == ["r3", "r2", "r1"]
    assert [e["run_id"] for e in recent("/r/nav", limit=2, root=root)] == ["r3", "r2"]
    assert [e["run_id"] for e in recent("/r/nav", limit=1, root=root)] == ["r3"]
    assert [e["run_id"] for e in recent("/r/nav", files=["a.py"], root=root)] == ["r3", "r1"]
    assert [e["run_id"] for e in recent("/r/nav", files=["c.py", "zzz.py"], root=root)] == ["r3"]
    assert [e["run_id"] for e in recent("/r/nav", files={"b.py"}, root=root)] == ["r2"]
    assert recent("/r/nav", files=["nope.py"], root=root) == []
    assert recent("/r/nav", files=[], root=root) == []      # a given (empty) filter matches nothing
    assert recent("/r/nav", limit=0, root=root) == []
    assert recent("/r/nav", root=root)[0]["reason_code"] is None
    assert recent("/r/nav", root=root)[2]["reason_code"] == "unsafe"
    # root=None resolves like root_for(): the env root, else DEFAULT_ROOT
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", root)
    assert len(recent("/r/nav")) == 3
    # accepted risk (B3 reads `repo` to disambiguate): two repos with one basename share a file
    same = recent("/elsewhere/nav", root=root)
    assert len(same) == 3 and {e["repo"] for e in same} == {"/r/nav"}


def test_switch_off_writes_nothing(tmp_path, monkeypatch):
    # guard: Settings default False, env unset -> no directory, no file, no log line
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY", raising=False)
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "mem"))
    with capture_logs() as logs:
        assert remember(SimpleNamespace(repo_memory=False, repo_memory_root=str(tmp_path / "mem")),
                        "/r/nav", kind="failure", run_id="r", proposal_id="p", text="x") is False
        assert remember(None, "/r/nav", kind="failure", run_id="r", proposal_id="p", text="x") is False
        # a settings object without the field (test_cli_reject_reason's SimpleNamespace) reads the env
        assert remember(SimpleNamespace(outcome_path="o"), "/r/nav", kind="failure", run_id="r",
                        proposal_id="p", text="x") is False
        # repo falsy while off: silent (no repo_memory.no_repo)
        assert remember(None, None, kind="failure", run_id="r", proposal_id="p") is False
    assert not (tmp_path / "mem").exists()
    assert not any(l["event"].startswith("repo_memory.") for l in logs)
    assert enabled(None) is False
    assert enabled(SimpleNamespace(repo_memory=False)) is False
    assert enabled(SimpleNamespace(repo_memory=True)) is True
    for v in ("1", "true", " True "):
        monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", v)
        assert enabled(None) is True, v
        assert enabled(SimpleNamespace(outcome_path="o")) is True, v
    # a Settings value wins over the env (one source of truth for the daemon)
    assert enabled(SimpleNamespace(repo_memory=False)) is False
    # the CLI path is lenient: an unparseable value is OFF (Settings.from_env raises instead)
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "maybe")
    assert enabled(None) is False
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY", "0")
    assert enabled(None) is False


def test_write_failure_warns_and_never_raises(tmp_path):
    clash = tmp_path / "afile"
    clash.write_text("x")
    s = SimpleNamespace(repo_memory=True, repo_memory_root=str(clash / "sub"))  # root under a FILE
    with capture_logs() as logs:
        ok = remember(s, "/r/nav", kind="failure", run_id="r", proposal_id="p", text="boom")
    assert ok is False
    failed = _events(logs, "repo_memory.write_failed")
    assert len(failed) == 1
    assert failed[0]["log_level"] == "warning"
    assert failed[0]["repo"] == "nav" and failed[0]["kind"] == "failure"
    assert failed[0]["error"] in ("NotADirectoryError", "FileExistsError")
    # class name only: no path and no exception text in the log line
    assert not any("/r/nav" in str(v) or "afile" in str(v) for v in failed[0].values())


def test_unknown_kind_is_refused_without_raising(tmp_path):
    assert KINDS == ("decline", "failure", "audit")
    s = _settings(tmp_path)
    with capture_logs() as logs:
        assert remember(s, "/r/nav", kind="note", run_id="r", proposal_id="p", text="x") is False
    failed = _events(logs, "repo_memory.write_failed")
    assert len(failed) == 1 and failed[0]["error"] == "ValueError" and failed[0]["kind"] == "note"
    assert not (tmp_path / "mem" / "nav.jsonl").exists()


def test_no_repo_warns_only_when_enabled(tmp_path):
    s = _settings(tmp_path)
    with capture_logs() as logs:
        assert remember(s, None, kind="failure", run_id="r", proposal_id="p") is False
        assert remember(s, "", kind="audit", run_id="r", proposal_id="p") is False
    warned = _events(logs, "repo_memory.no_repo")
    assert len(warned) == 2 and all(w["log_level"] == "warning" for w in warned)
    assert {w["kind"] for w in warned} == {"failure", "audit"}
    assert not (tmp_path / "mem").exists()


def test_files_keep_affected_first_then_touched_capped_at_50(tmp_path):
    s = _settings(tmp_path)
    touched = [f"t{i:03d}.py" for i in range(60)]
    affected = ["z_aff.py", "a_aff.py", "t005.py"]
    assert remember(s, "/r/nav", kind="decline", run_id="r", proposal_id="p",
                    files=set(touched + ["dup.py", "dup.py"]), affected_files=affected) is True
    e = recent("/r/nav", root=root_for(s))[0]
    assert len(e["files"]) == 50
    # the proposal's own files come first (sorted) and are never dropped by the cap
    assert e["files"][:3] == ["a_aff.py", "t005.py", "z_aff.py"]
    rest = sorted(set(touched + ["dup.py"]) - set(affected))
    assert e["files"][3:] == rest[:47]
    assert e["files"].count("t005.py") == 1
    # text is capped at 2000 chars
    remember(s, "/r/nav", kind="failure", run_id="r2", proposal_id="p", text="x" * 2500)
    assert len(recent("/r/nav", root=root_for(s))[0]["text"]) == 2000
    # affected_files alone is enough
    remember(s, "/r/nav", kind="audit", run_id="r3", proposal_id="p", affected_files=["b.py", "a.py"])
    assert recent("/r/nav", root=root_for(s))[0]["files"] == ["a.py", "b.py"]


def test_recent_skips_corrupt_lines_and_missing_file(tmp_path):
    root = tmp_path / "mem"
    assert recent("/r/nav", root=str(root)) == []                 # no directory at all
    root.mkdir()
    assert recent("/r/nav", root=str(root)) == []                 # no file yet
    p = root / "nav.jsonl"
    p.write_text('{"run_id": "good", "kind": "failure", "files": ["a.py"]}\n'
                 '\n'
                 '[1, 2]\n'
                 'not json\n'
                 '{"run_id": "partial", "files":', encoding="utf-8")   # crash mid-write, no newline
    assert [e["run_id"] for e in recent("/r/nav", root=str(root))] == ["good"]
    assert [e["run_id"] for e in recent("/r/nav", files=["a.py"], root=str(root))] == ["good"]
    # a corrupt "files" value never makes the filter raise
    p.write_text('{"run_id": "s", "files": "a.py"}\n{"run_id": "t"}\n', encoding="utf-8")
    assert recent("/r/nav", files=["a.py"], root=str(root)) == []
    assert [e["run_id"] for e in recent("/r/nav", root=str(root))] == ["t", "s"]
    # an unreadable path (a directory where the file should be) is an OSError -> []
    (root / "dir.jsonl").mkdir()
    assert recent("/x/dir", root=str(root)) == []


def test_root_for_resolves_settings_then_env_then_default(tmp_path, monkeypatch):
    from navigator.config import Settings
    monkeypatch.delenv("NAVIGATOR_REPO_MEMORY_ROOT", raising=False)
    monkeypatch.setattr(repo_memory, "DEFAULT_ROOT", str(tmp_path / "dflt"))
    assert root_for(None) == str(tmp_path / "dflt")
    assert root_for(SimpleNamespace(repo_memory_root=None)) == str(tmp_path / "dflt")
    assert root_for(SimpleNamespace(outcome_path="o")) == str(tmp_path / "dflt")
    monkeypatch.setenv("NAVIGATOR_REPO_MEMORY_ROOT", str(tmp_path / "env"))
    assert root_for(None) == str(tmp_path / "env")          # resolved at call time, not import time
    assert root_for(SimpleNamespace(repo_memory_root="")) == str(tmp_path / "env")
    assert root_for(SimpleNamespace(repo_memory_root=str(tmp_path / "set"))) == str(tmp_path / "set")
    # Settings never bakes the live literal in: no root -> env/default at call time
    assert Settings(repo_memory=True).repo_memory_root is None
    assert root_for(Settings(repo_memory=True)) == str(tmp_path / "env")
    # the module default is the documented live path (patched under tmp by conftest for every test)
    source = Path(repo_memory.__file__).read_text(encoding="utf-8")
    assert f'DEFAULT_ROOT = "{LIVE_ROOT}"' in source
    assert repo_memory.DEFAULT_ROOT != LIVE_ROOT   # conftest isolation is in force for this test too
    assert path_for("/a/b/nav", "/root") == Path("/root/nav.jsonl")


# ---- reviewer memory B3: the builder/verifier view (compact) ---------------------------------

def _entry(i, text="t", files=("a.py",), title="title", kind="failure", code=None):
    return {"ts": f"2026-11-10T00:00:{i:02d}+00:00", "kind": kind, "repo": "/r/nav", "run_id": f"r{i}",
            "proposal_id": "p", "reason_code": code, "text": text, "files": list(files),
            "source": "executor", "verified_sha": "v" * 40, "base_sha": "b" * 40, "title": title}


def _size(e):
    return len(json.dumps(e, separators=(",", ":")))


def test_compact_caps_entries_chars_text_and_files():
    compact = getattr(repo_memory, "compact", None)
    assert compact, "repo_memory.compact is missing"
    assert (repo_memory.CONTEXT_ENTRIES, repo_memory.CONTEXT_CHARS, repo_memory.CONTEXT_TEXT,
            repo_memory.CONTEXT_FILES) == (8, 2500, 300, 6)
    # 9 small entries, newest first as recent() hands them over -> the 8 newest, order kept
    nine = [_entry(9 - i) for i in range(9)]
    out = compact(nine)
    assert [e["ts"] for e in out] == [e["ts"] for e in nine[:8]]
    # the view carries only what a builder can use: no shas, source, run_id or repo
    assert all(set(e) == {"kind", "reason_code", "text", "files", "ts", "title"} for e in out)
    assert out[0]["kind"] == "failure" and out[0]["reason_code"] is None and out[0]["files"] == ["a.py"]
    assert compact([]) == []
    # text cut to 300, title to 80; reason_code passes through
    long = compact([_entry(1, text="x" * 301, title="T" * 100, kind="decline", code="unsafe")])
    assert len(long[0]["text"]) == 300 and len(long[0]["title"]) == 80
    assert long[0]["reason_code"] == "unsafe" and long[0]["kind"] == "decline"
    # 7 files -> 6: the proposal's own (affected) files first, then the rest, each in stored order
    seven = compact([_entry(1, files=["a", "b", "c", "d", "e", "f", "g"])], affected=("g", "e", "zzz"))
    assert seven[0]["files"] == ["e", "g", "a", "b", "c", "d"]
    # defensive coercion: text not a str, files not a list / not strings
    odd = compact([_entry(1, text=None, files="a.py"), _entry(0, text=42, files=[1, None, "b.py"])])
    assert odd[0]["text"] == "" and odd[0]["files"] == []
    assert odd[1]["text"] == "42" and odd[1]["files"] == ["1", "None", "b.py"]
    # a title of None stays None
    assert compact([_entry(1, title=None)])[0]["title"] is None


def test_compact_stops_at_the_first_entry_over_the_char_budget():
    compact = getattr(repo_memory, "compact", None)
    assert compact, "repo_memory.compact is missing"
    # each compact entry is ~500 chars: 8 would be ~4000 > 2500, so the view stops early, newest kept
    fat = [_entry(9 - i, text="x" * 300, title="T" * 80, files=[f"f{i}_" + "p" * 20 for i in range(6)])
           for i in range(9)]
    out = compact(fat)
    assert 0 < len(out) < 8
    assert [e["ts"] for e in out] == [e["ts"] for e in fat[:len(out)]]   # a prefix: newest first
    total = sum(_size(e) for e in out)
    assert total <= 2500
    nxt = compact([fat[len(out)]])[0]
    assert total + _size(nxt) > 2500          # the first dropped entry is the one that would overflow
    # STOP, do not skip: a small entry behind an oversize one is dropped with it
    big = _entry(5, files=["F" * 600 for _ in range(6)])
    small = _entry(4)
    assert compact([_entry(6), big, small]) == compact([_entry(6)])
    # a single newest entry over the budget -> []
    assert compact([big]) == []
    assert compact([big, small]) == []
