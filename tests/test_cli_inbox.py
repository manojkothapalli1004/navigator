"""`navigator inbox [--since UPDATE_ID]`: other tools read operator messages from the
inbox file written by TelegramInbox instead of calling getUpdates themselves."""
import io
import json

import pytest

from navigator.cli import COMMANDS, run_cli
from navigator.clients.act_client import FakeACTClient


def _cli(*argv):
    out = io.StringIO()
    rc = run_cli(list(argv), act=FakeACTClient(), out=out)
    return rc, out.getvalue()


def _entry(uid, text, **extra):
    return json.dumps({"update_id": uid, "date": 1760000000 + uid, "text": text, **extra},
                      separators=(",", ":"))


@pytest.fixture
def inbox_path(tmp_path, monkeypatch):
    p = tmp_path / "inbox.jsonl"
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_INBOX_PATH", str(p))
    return p


def test_inbox_is_a_cli_command():
    assert "inbox" in COMMANDS


def test_inbox_prints_json_lines_newest_last(inbox_path):
    lines = [_entry(1, "hello"), _entry(2, "/merge run-x", ignored="stale"), _entry(3, "later")]
    inbox_path.write_text("\n".join(lines) + "\n")
    rc, out = _cli("inbox")
    assert rc == 0
    assert out.splitlines() == lines
    assert [json.loads(l)["update_id"] for l in out.splitlines()] == [1, 2, 3]


def test_inbox_since_filters_out_older_and_equal_update_ids(inbox_path):
    inbox_path.write_text("\n".join(_entry(i, f"m{i}") for i in (1, 2, 3, 4)) + "\n")
    rc, out = _cli("inbox", "--since", "2")
    assert rc == 0
    assert [json.loads(l)["update_id"] for l in out.splitlines()] == [3, 4]
    rc, out = _cli("inbox", "--since", "4")
    assert rc == 0 and out == ""
    rc, out = _cli("inbox", "--since", "0")
    assert rc == 0
    assert [json.loads(l)["update_id"] for l in out.splitlines()] == [1, 2, 3, 4]


def test_inbox_missing_file_prints_nothing(inbox_path):
    assert not inbox_path.exists()
    assert _cli("inbox") == (0, "")
    assert _cli("inbox", "--since", "5") == (0, "")


def test_inbox_invalid_utf8_and_partial_lines_never_raise(inbox_path):
    good1 = _entry(1, "first").encode()
    bad_bytes = b'{"update_id":2,"text":"\xff\xfe broken"}'   # not UTF-8
    partial = b'{"update_id":3,"text":'                        # crash mid-write
    good4 = _entry(4, "last").encode()
    inbox_path.write_bytes(b"\n".join([good1, bad_bytes, b"not json at all", partial, good4])
                           + b"\n")
    rc, out = _cli("inbox")
    assert rc == 0
    printed = [json.loads(l) for l in out.splitlines()]
    ids = [e["update_id"] for e in printed]
    assert ids[0] == 1 and ids[-1] == 4 and 3 not in ids
    assert "not json" not in out
    # the undecodable bytes were replaced, not raised; filtering still works
    rc, out = _cli("inbox", "--since", "2")
    assert rc == 0
    assert [json.loads(l)["update_id"] for l in out.splitlines()] == [4]


def test_inbox_bad_arguments(inbox_path):
    assert _cli("inbox", "--since")[0] == 2
    assert _cli("inbox", "--since", "x")[0] == 2
    assert _cli("inbox", "5")[0] == 2
    assert _cli("inbox", "--since", "1", "extra")[0] == 2
