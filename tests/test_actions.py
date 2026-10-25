"""Actions v1 (operator 2026-10-24): a manual step may carry ONE pinned script that the
operator runs from Telegram with 'run <id>' - no arguments, refused when the script
changed, every attempt logged, nothing else runnable."""
import fcntl
import json
import os
import shutil
import signal
import time
from pathlib import Path

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.observability import actions, decisions
from navigator.observability.telegram_proposals import (ProposalCommand,
                                                        handle_operator_message,
                                                        parse_proposal_command)

CHAT = "4242"
NOW = 1_800_000_000.0


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


@pytest.fixture
def adir(tmp_path, monkeypatch):
    d = tmp_path / "actions"
    d.mkdir()
    d.chmod(0o700)
    monkeypatch.setenv("NAVIGATOR_ACTIONS_DIR", str(d))
    monkeypatch.setenv("NAVIGATOR_ACTION_LOG_PATH", str(tmp_path / "logs" / "action_runs.jsonl"))
    monkeypatch.setenv("NAVIGATOR_MANUAL_DECISIONS_PATH", str(tmp_path / "manual.json"))
    monkeypatch.setenv("NAVIGATOR_DECISIONS_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("NAVIGATOR_TRIAGE_PATH", str(tmp_path / "triage.json"))
    yield d
    d.chmod(0o700)


def _script(d, name, body, mode=0o700):
    p = d / name
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(mode)
    return p


def _add(d, item_id="step1", body=None):
    """Pin a script for item_id; by default it touches a marker file next to the dir."""
    marker = d.parent / f"{item_id}.marker"
    p = _script(d, f"{item_id}.sh", f"touch {marker}" if body is None else body)
    decisions.add_manual(decisions.manual_path(), item_id, f"Install {item_id}", "do it",
                         script=str(p))
    return p, marker


def _log():
    path = actions.action_log_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines() if line]


def _msg(text, **extra):
    m = {"chat": {"id": int(CHAT), "type": "private"}, "from": {"id": int(CHAT)},
         "date": int(NOW), "text": text}
    m.update(extra)
    return m


# ---- a) the happy path ------------------------------------------------------------

def test_run_executes_the_pinned_script_and_logs_it(adir):
    p, marker = _add(adir)
    item = decisions.read_manual(decisions.manual_path())[0]
    assert item["script"] == str(p) and item["sha256"] == actions.sha256_file(p)
    reply = actions.run_action("step1", update_id=7, now=NOW)
    assert reply.startswith("ran step1: rc=0")
    assert marker.exists()
    log = _log()
    assert len(log) == 2
    assert log[0]["outcome"] == "started" and log[0]["rc"] is None and log[0]["id"] == "step1"
    assert log[0]["sha256"] == item["sha256"] and log[0]["update_id"] == 7
    assert log[1]["outcome"] == "ran" and log[1]["rc"] == 0 and log[1]["id"] == "step1"
    assert log[1]["sha256"] == item["sha256"] and log[1]["update_id"] == 7
    assert os.stat(actions.action_log_path()).st_mode & 0o777 == 0o600
    # the item stays until the operator says 'done'
    assert decisions.read_manual(decisions.manual_path())[0]["id"] == "step1"


# ---- b) the script changed ---------------------------------------------------------

def test_changed_script_is_refused(adir):
    p, marker = _add(adir)
    p.write_text(p.read_text() + "echo tampered\n")
    reply = actions.run_action("step1", now=NOW)
    assert "script changed" in reply and not reply.startswith("ran")
    assert not marker.exists()
    assert [r["outcome"] for r in _log()] == ["refused"]
    assert "script changed" in _log()[0]["reason"]


# ---- c) what may be pinned ---------------------------------------------------------

def _add_raises(path, reason):
    with pytest.raises(ValueError, match=reason):
        decisions.add_manual(decisions.manual_path(), "bad", "t", "h", script=str(path))
    assert decisions.read_manual(decisions.manual_path()) == []


def test_add_refuses_unsafe_scripts(adir, tmp_path):
    good = _script(adir, "good.sh", "true")
    link = adir / "link.sh"
    os.symlink(good, link)
    _add_raises(link, "symlink")
    other = tmp_path / "other"
    other.mkdir()
    other.chmod(0o700)
    _add_raises(_script(other, "o.sh", "true"), "outside actions dir")
    _add_raises(_script(adir, "gw.sh", "true", mode=0o720), "group/world-writable")
    _add_raises(_script(adir, "nx.sh", "true", mode=0o600), "not executable")
    adir.chmod(0o777)
    _add_raises(good, "actions dir unsafe")
    adir.chmod(0o700)
    decisions.add_manual(decisions.manual_path(), "ok", "t", "h", script=str(good))
    assert decisions.read_manual(decisions.manual_path())[0]["id"] == "ok"


def test_script_made_writable_after_approval_is_refused(adir):
    p, marker = _add(adir)
    p.chmod(0o722)
    reply = actions.run_action("step1", now=NOW)
    assert "group/world-writable" in reply
    assert not marker.exists()
    assert _log()[-1]["outcome"] == "refused"


def test_cli_add_with_script(adir):
    p = _script(adir, "c.sh", "true")
    assert decisions._cli(["add", "cli1", "title", "how", "--script", str(p)]) == 0
    assert decisions.read_manual(decisions.manual_path())[0]["sha256"] == actions.sha256_file(p)
    assert decisions._cli(["add", "cli2", "title", "how", "--script",
                           str(adir / "nope.sh")]) == 2
    assert [it["id"] for it in decisions.read_manual(decisions.manual_path())] == ["cli1"]


# ---- d) rate limit, e) lock, f) timeout ---------------------------------------------

def test_one_run_per_minute(adir):
    _add(adir)
    assert actions.run_action("step1", now=NOW).startswith("ran step1")
    assert "ran less than 60s ago" in actions.run_action("step1", now=NOW + 10)
    assert actions.run_action("step1", now=NOW + 61).startswith("ran step1")
    assert [r["outcome"] for r in _log()] == ["started", "ran", "refused", "started", "ran"]


def test_a_held_lock_refuses_the_run(adir):
    _, marker = _add(adir)
    fd = os.open(str(adir / ".run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        reply = actions.run_action("step1", now=NOW)
    finally:
        os.close(fd)
    assert "another action is running" in reply
    assert not marker.exists()
    assert _log()[-1]["outcome"] == "refused"


def test_timeout_is_reported_and_logged(adir, monkeypatch):
    _add(adir, "slow", body="sleep 5")
    monkeypatch.setattr(actions, "RUN_TIMEOUT_S", 1)
    reply = actions.run_action("slow", now=NOW)
    assert "timed out" in reply
    assert _log()[-1]["outcome"] == "timeout"


# ---- g) the environment ----------------------------------------------------------

def test_script_sees_only_the_safe_environment(adir, monkeypatch):
    monkeypatch.setenv("NAV_TEST_SECRET", "leak")
    _add(adir, "env", body='echo "$PATH"\necho "$NAV_TEST_SECRET"')
    reply = actions.run_action("env", now=NOW)
    assert actions.SAFE_PATH in reply
    assert "leak" not in reply


# ---- h) nothing else is runnable -------------------------------------------------

def test_unknown_or_unscripted_items_run_nothing(adir):
    decisions.add_manual(decisions.manual_path(), "plain", "Plug the charger", "walk over")
    assert actions.run_action("nope", now=NOW) == "no manual step nope"
    assert actions.run_action("plain", now=NOW).startswith("no script - do it yourself:")
    assert [r["outcome"] for r in _log()] == ["refused", "refused"]
    assert not any(p.name.endswith(".marker") for p in adir.parent.iterdir())
    assert actions.show_action("nope") == "no manual step nope"
    assert actions.show_action("plain").startswith("no script - do it yourself:")


# ---- i) from Telegram ------------------------------------------------------------

def test_run_from_telegram(adir):
    _, marker = _add(adir)
    rec = _Recorder()
    assert handle_operator_message(_msg("run step1"), FakeACTClient(), rec, CHAT,
                                   now=NOW, update_id=11) is True
    assert marker.exists()
    assert any("ran step1" in t for t in rec.sent)
    assert _log()[-1]["update_id"] == 11
    rec = _Recorder()
    assert handle_operator_message(_msg("show step1"), FakeACTClient(), rec, CHAT,
                                   now=NOW) is True
    assert "#!/bin/sh" in "\n".join(rec.sent)


@pytest.mark.parametrize("text,cmd", [
    ("run step1", ProposalCommand("run", ("step1",))),
    ("/Show step1", ProposalCommand("show", ("step1",))),
    ("run", None),
    ("run a b", None),
    ("show", None),
    ("run ../etc", None),
    ("run step1; rm", None),
])
def test_parse_run_and_show(text, cmd):
    assert parse_proposal_command(text) == cmd


def test_forwarded_run_is_ignored(adir):
    _, marker = _add(adir)
    rec = _Recorder()
    assert handle_operator_message(_msg("run step1", forward_date=int(NOW)), FakeACTClient(),
                                   rec, CHAT, now=NOW) is True
    assert "command ignored" in rec.sent[-1] and "forwarded" in rec.sent[-1]
    assert not marker.exists()
    assert _log() == []


# ---- j) show and the decisions list ----------------------------------------------

def test_show_prints_the_script_capped(adir):
    p, _ = _add(adir)
    pinned = decisions.read_manual(decisions.manual_path())[0]["sha256"]
    out = actions.show_action("step1")
    lines = out.split("\n")
    assert lines[0] == "Install step1"
    assert lines[1] == f"script: {p}"
    assert lines[2] == f"pinned sha256: {pinned[:12]}"
    assert p.read_text().strip() in out
    _add(adir, "long", body="\n".join(f"echo line {i}" for i in range(300)))
    out = actions.show_action("long")
    assert out.endswith("...") and "echo line 0" in out and "echo line 299" not in out
    body = out.split("\n", 3)[3]
    assert len(body) == actions.MAX_OUTPUT_CHARS + 3


def test_collect_lists_scripted_and_plain_steps(adir):
    _add(adir)
    decisions.add_manual(decisions.manual_path(), "plain", "Plug the charger", "walk over")
    items = {d.key: d for d in decisions.collect(FakeACTClient())}
    assert items["manual:step1"].reply.startswith("run step1")
    assert "show step1" in items["manual:step1"].reply
    assert items["manual:plain"].reply.startswith("do yourself:")


# ---- k) output is capped and never logged ----------------------------------------

def test_output_is_tailed_in_the_reply_and_absent_from_the_log(adir):
    _add(adir, "loud", body="awk 'BEGIN{for(i=0;i<5000;i++)printf \"%d\", i%10}'")
    reply = actions.run_action("loud", now=NOW)
    head, _, body = reply.partition("\n")
    assert head == "ran loud: rc=0"
    assert len(body) == actions.MAX_OUTPUT_CHARS
    assert body == "0123456789" * 150
    with open(actions.action_log_path(), encoding="utf-8") as f:
        raw = f.read()
    assert "0123456789" not in raw
    assert "output" not in _log()[-1]


# ---- l) v1.1: started line, binary output, process group, guarded show -------------

def _rewrite_script(item_id, new_path):
    """Point the manual item's 'script' at new_path behind decisions' back (the pin stays)."""
    mp = decisions.manual_path()
    with open(mp, encoding="utf-8") as f:
        data = json.load(f)

    def walk(o):
        if isinstance(o, dict):
            if o.get("id") == item_id and "script" in o:
                o["script"] = new_path
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert decisions.read_manual(mp)[0]["script"] == new_path


def test_binary_output_is_survived_and_logged(adir):
    _add(adir, body="printf '\\377\\376ok'")
    assert actions.run_action("step1", now=NOW).startswith("ran step1: rc=0")
    assert [r["outcome"] for r in _log()] == ["started", "ran"]
    assert "ran less than 60s ago" in actions.run_action("step1", now=NOW + 1)


def test_unwritable_log_refuses_the_run(adir, tmp_path, monkeypatch):
    _, marker = _add(adir)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setenv("NAVIGATOR_ACTION_LOG_PATH", str(ro / "log.jsonl"))
    try:
        reply = actions.run_action("step1", now=NOW)
    finally:
        ro.chmod(0o700)
    assert reply == "not run: action log unwritable"
    assert not marker.exists()


def test_a_started_line_counts_for_the_rate_limit(adir):
    _, marker = _add(adir)
    lp = Path(actions.action_log_path())
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps({"ts": NOW - 5, "id": "step1", "outcome": "started"}) + "\n",
                  encoding="utf-8")
    assert "ran less than 60s ago" in actions.run_action("step1", now=NOW)
    assert not marker.exists()


def test_garbled_future_and_infinite_log_lines_do_not_block(adir):
    _, marker = _add(adir)
    lp = Path(actions.action_log_path())
    lp.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": NOW - 3600, "id": "step1", "outcome": "ran"}),
             json.dumps({"ts": NOW + 10000, "id": "step1", "outcome": "ran"}),
             '{"id": "step1", "outcome": "ran", "ts": Infinity}']
    lp.write_bytes(b"\xff\xfe garbage\n" + ("\n".join(lines) + "\n").encode("utf-8"))
    assert actions.run_action("step1", now=NOW).startswith("ran step1")
    assert marker.exists()


def _wait_gone(pid, seconds=2.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_timeout_kills_the_whole_process_group(adir, tmp_path, monkeypatch):
    pidfile = tmp_path / "gc.pid"
    _add(adir, body=f"sleep 30 & echo $! > {pidfile}; wait")
    monkeypatch.setattr(actions, "RUN_TIMEOUT_S", 1)
    reply = actions.run_action("step1", now=NOW)
    assert "timed out" in reply
    pid = int(pidfile.read_text().strip())
    gone = _wait_gone(pid)
    if not gone:
        _kill(pid)
    assert gone


def test_an_escaped_process_does_not_hang_the_bot(adir, tmp_path, monkeypatch):
    pidfile = tmp_path / "esc.pid"
    # the setsid child inherits stdout and leaves the process group
    body = ("/usr/bin/perl -MPOSIX -e 'if (fork() == 0) { POSIX::setsid(); "
            f'open(F, ">", "{pidfile}"); print F $$; close F; sleep 30; exit 0 }}'
            "'; echo parent-done")
    _add(adir, body=body)
    monkeypatch.setattr(actions, "RUN_TIMEOUT_S", 1)
    pids = []

    def note_pid():
        try:
            pids.append(int(pidfile.read_text().strip()))
        except (OSError, ValueError):
            pass

    try:
        start = time.monotonic()
        reply = actions.run_action("step1", now=NOW)
        assert time.monotonic() - start < 15
        assert "timed out" in reply
        assert _log()[-1]["outcome"] == "timeout"
        note_pid()
        reply = actions.run_action("step1", now=NOW + 61)
        assert "another action is running" not in reply
    finally:
        note_pid()
        for pid in set(pids):
            _kill(pid)


def test_an_unsignalable_child_does_not_hang_the_bot(adir, tmp_path, monkeypatch):
    # v1.2: os.killpg fails (the child exec'd a setuid program), so nothing is killed;
    # the pipes are closed after REAP_TIMEOUT_S and the last wait is bounded too
    pidfile = tmp_path / "child.pid"
    _add(adir, body=f"echo $$ > {pidfile}; exec sleep 30")
    monkeypatch.setattr(actions, "RUN_TIMEOUT_S", 1)
    monkeypatch.setattr(actions, "REAP_TIMEOUT_S", 1)

    def raiser(pgid, sig):
        raise PermissionError

    monkeypatch.setattr(actions.os, "killpg", raiser)
    try:
        t0 = time.monotonic()
        reply = actions.run_action("step1", now=NOW)
        assert time.monotonic() - t0 < 10
        assert reply.startswith("ran step1: timed out after 1s")
        assert reply.endswith("(process still running)")
        assert _log()[-1]["outcome"] == "timeout"
    finally:
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            _kill(pid)  # the real os.kill: only os.killpg was patched


def test_relative_script_path_is_refused(adir):
    _, marker = _add(adir)
    _rewrite_script("step1", "step1.sh")
    assert actions.run_action("step1", now=NOW) == "not run: not absolute"
    assert not marker.exists()
    assert actions.show_action("step1") == "not shown: not absolute"


def test_symlinked_script_is_refused_at_run_time(adir):
    p, marker = _add(adir)
    real = adir.parent / "real.sh"
    shutil.copy2(p, real)
    real.chmod(0o700)
    p.unlink()
    os.symlink(real, p)
    assert actions.run_action("step1", now=NOW) == "not run: symlink"
    assert not marker.exists()


def test_script_moved_outside_the_actions_dir_is_refused(adir):
    p, marker = _add(adir)
    out = adir.parent / "out.sh"
    shutil.copy2(p, out)
    out.chmod(0o700)
    assert actions.sha256_file(out) == actions.sha256_file(p)
    _rewrite_script("step1", str(out))
    assert actions.run_action("step1", now=NOW) == "not run: outside actions dir"
    assert not marker.exists()


def test_script_made_non_executable_is_refused(adir):
    p, marker = _add(adir)
    p.chmod(0o600)
    assert actions.run_action("step1", now=NOW) == "not run: not executable"
    assert not marker.exists()


def test_script_not_owned_by_the_user_is_refused(adir, monkeypatch):
    _, marker = _add(adir)
    real_uid = os.getuid()
    monkeypatch.setattr(actions.os, "getuid", lambda: real_uid + 1)
    reply = actions.run_action("step1", now=NOW)
    assert reply.startswith("not run: ")
    assert reply in ("not run: actions dir unsafe", "not run: not owned by you")
    assert not marker.exists()


def test_show_refuses_paths_that_fail_the_checks(adir):
    _add(adir)
    _rewrite_script("step1", "/etc/hosts")
    out = actions.show_action("step1")
    assert out.startswith("not shown: ")
    assert "localhost" not in out


def test_show_warns_when_the_script_changed_since_approval(adir):
    p, _ = _add(adir)
    assert "WARNING: changed since approval" not in actions.show_action("step1")
    p.write_text(p.read_text() + "echo tampered\n", encoding="utf-8")
    p.chmod(0o700)
    out = actions.show_action("step1")
    assert "WARNING: changed since approval - 'run' will refuse it" in out
    assert "echo tampered" in out
