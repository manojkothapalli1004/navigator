import fcntl
import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from navigator.conductor import join as joinmod
from navigator.conductor import transcript
from navigator.conductor.store import Store

SID = "sess-abc"


def _setup(tmp_path, sid=SID):
    root = tmp_path / "root"
    st = Store(root)
    if sid:
        st.set_session_id(sid)
    home = tmp_path / "home"
    path = transcript.transcript_path(sid or "x", home=home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": "old"}}) + "\n")
    return root, home, path


def _append(path, *records):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class Poster:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, body):
        if self.fail:
            raise httpx.ConnectError("no socket")
        self.calls.append(body)
        return {}


def _join(root, home, run, post=None, out=None):
    return joinmod.join(root=root, run=run, post=post or Poster(), out=out or io.StringIO(), home=home)


def test_join_without_session_refuses(tmp_path):
    root, home, _ = _setup(tmp_path, sid=None)
    out, ran = io.StringIO(), []
    assert _join(root, home, lambda *a, **k: ran.append(a), out=out) == 1
    assert ran == []
    assert out.getvalue().strip() == ("No shared conversation yet — send one message from the website "
                                      "or Telegram first.")


def test_join_runs_resume_in_workspace(tmp_path):
    root, home, _ = _setup(tmp_path)
    seen, post = [], Poster()
    rc = _join(root, home, lambda argv, cwd: seen.append((argv, cwd)) or 0, post=post)
    assert rc == 0 and seen == [(["claude", "--resume", SID], "/Users/Manoj/calwbot")]
    assert post.calls == [{"state": "joined"}, {"state": "left", "entries": []}]


def test_join_imports_only_new_records(tmp_path):
    root, home, path = _setup(tmp_path)
    post = Poster()

    def run(argv, cwd):
        _append(path, {"type": "user", "message": {"content": "hi from terminal"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})
        return 0
    _join(root, home, run, post=post)
    assert post.calls[-1] == {"state": "left", "entries": [{"kind": "user", "text": "hi from terminal"},
                                                          {"kind": "assistant", "text": "hello"}]}


def test_join_holds_lock_during_session(tmp_path):
    root, home, _ = _setup(tmp_path)

    def run(argv, cwd):
        with open(root / "turn.lock", "w") as fh:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 0
    _join(root, home, run)
    with open(root / "turn.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released afterwards


def test_join_waits_for_running_turn(tmp_path):
    root, home, _ = _setup(tmp_path)
    fh = open(root / "turn.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)  # a conductor turn is running

    def release():
        time.sleep(0.2); fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    threading.Thread(target=release).start()
    out, ran = io.StringIO(), []
    _join(root, home, lambda argv, cwd: ran.append(argv) or 0, out=out)
    assert "The chat service is answering — waiting for it to finish (Ctrl-C to cancel)…" in out.getvalue()
    assert ran


def test_join_with_conductor_down_warns_and_runs(tmp_path):
    root, home, _ = _setup(tmp_path)
    out, ran, post = io.StringIO(), [], Poster(fail=True)
    _join(root, home, lambda argv, cwd: ran.append(argv) or 0, post=post, out=out)
    assert ran and post.calls == []
    assert "Chat service not running — this terminal session won't show in the web history." in out.getvalue()


def test_left_posted_even_if_claude_crashes(tmp_path):
    root, home, _ = _setup(tmp_path)
    post = Poster()

    def run(argv, cwd):
        raise FileNotFoundError("claude")
    with pytest.raises(FileNotFoundError):
        _join(root, home, run, post=post)
    assert post.calls[-1] == {"state": "left", "entries": []}
    with open(root / "turn.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_long_entry_truncated_not_rejected(tmp_path):
    # cold audit: one >20000-char reply made the server 422 the whole import
    root, home, path = _setup(tmp_path)
    post = Poster()

    def run(argv, cwd):
        _append(path, {"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 25000}]}})
        return 0
    _join(root, home, run, post=post)
    entries = post.calls[-1]["entries"]
    assert len(entries) == 1 and len(entries[0]["text"]) <= 20000 and entries[0]["text"].endswith("…")


def test_session_id_read_after_waiting_for_lock(tmp_path):
    # cold audit: a turn we waited out may have started a fresh session
    root, home, _ = _setup(tmp_path)
    fh = open(root / "turn.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)

    def turn_finishes_with_new_session():
        time.sleep(0.2)
        Store(root).set_session_id("sess-new")
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    threading.Thread(target=turn_finishes_with_new_session).start()
    seen = []
    _join(root, home, lambda argv, cwd: seen.append(argv) or 0)
    assert seen == [["claude", "--resume", "sess-new"]]


def test_returns_claude_exit_code(tmp_path):
    root, home, _ = _setup(tmp_path)
    assert _join(root, home, lambda argv, cwd: 3) == 3


def test_main_dispatches_join(monkeypatch):
    from navigator.conductor import __main__ as cli
    seen = []
    monkeypatch.setattr(joinmod, "join", lambda root, socket: seen.append((root, socket)) or 7)
    assert cli.main(["join"]) == 7 and seen == [(cli.ROOT, cli.SOCKET)]
