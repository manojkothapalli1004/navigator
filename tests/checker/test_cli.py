import json
from types import SimpleNamespace

from navigator.checker import __main__ as cli
from navigator.checker import preview
from navigator.checker.browse import BrowserUnavailable, ConsoleUnreachable


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class FakeACT:
    def __init__(self):
        self.created = []

    def create_proposal(self, p):
        self.created.append(p)
        return p

    def get_proposal(self, pid):
        return None

    def list_runs_for_proposal(self, pid):
        return []


def snap(page, scroll=412):
    return ({"page": page, "viewport": "phone", "status": 200, "console_errors": [], "failed_requests": [],
             "scroll_width": scroll, "viewport_width": 412, "small_targets": [], "live_claims": [],
             "combos": [], "interactions": {}}, f"/tmp/{page.strip('/') or 'home'}.png")


def run(tmp_path, browse_fn, *args, act=None, notes=None, wait_fn=lambda: None):
    notes = notes or Notes()
    code = cli.main(["run", *args], browse_fn=browse_fn, act=act or FakeACT(), notifier=notes,
                    root=tmp_path, base_sha=lambda: "abc", wait_fn=wait_fn)
    return code, notes


def test_clean_run_exits_0_and_is_quiet(tmp_path):
    code, notes = run(tmp_path, lambda base, out: [snap("/")])
    assert code == 0 and notes.sent == []
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert summary["new"] == [] and summary["pages"] == 1


def test_faults_exit_1_and_message_once(tmp_path):
    browse_fn = lambda base, out: [snap("/trader", scroll=900)]
    code, notes = run(tmp_path, browse_fn)
    assert code == 1 and len(notes.sent) == 1 and "hscroll on /trader" in notes.sent[0]
    code, notes2 = run(tmp_path, browse_fn)
    assert code == 1 and notes2.sent == []   # nothing changed: quiet


def test_unreachable_exits_2_with_one_line(tmp_path):
    def boom(base, out):
        raise ConsoleUnreachable("http://127.0.0.1:3000: refused")
    code, notes = run(tmp_path, boom)
    assert code == 2 and len(notes.sent) == 1 and "unreachable" in notes.sent[0]


def test_chrome_launch_failure_exits_2(tmp_path):
    def boom(base, out):
        raise BrowserUnavailable("chrome not found")
    act = FakeACT()
    code, notes = run(tmp_path, boom, act=act)
    assert code == 2 and "could not start Chrome" in notes.sent[0] and act.created == []


def test_files_only_with_file_flag(tmp_path):
    browse_fn = lambda base, out: [snap("/trader", scroll=900)]
    act = FakeACT()
    run(tmp_path, browse_fn, act=act)
    assert act.created == []
    run(tmp_path, browse_fn, "--file", act=act)
    assert len(act.created) == 1


def test_preview_stop_only_kills_port_owner(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout="4242\n4343\n", returncode=0)

    killed = []
    monkeypatch.setattr(preview.subprocess, "run", fake_run)
    monkeypatch.setattr(preview.os, "kill", lambda pid, sig: killed.append(pid))
    preview.stop(3001)
    assert calls[0] == [preview.LSOF, "-tiTCP:3001", "-sTCP:LISTEN"]
    assert killed == [4242, 4343]


def test_latest_run_is_by_start_time_not_list_order(tmp_path):
    from navigator.checker.store import FindingStore
    from navigator.checker.checks import Finding
    store = FindingStore(tmp_path / "s.json")
    f = Finding("/", "phone", "hscroll", "body", "x")
    store.record([f], "2026-10-31T20:00:00+00:00")
    store.mark_filed(f.fingerprint, "prop-1")
    newer = SimpleNamespace(status="completed", started_at="2026-10-31T21:00:00+00:00")
    older = SimpleNamespace(status="failed", started_at="2026-10-31T20:30:00+00:00")
    act = FakeACT()
    act.list_runs_for_proposal = lambda pid: [newer, older]
    assert cli._statuses(act, store) == {"prop-1": "completed"}


def test_unexpected_error_exits_2_with_a_message(tmp_path):
    def boom(base, out):
        raise RuntimeError("weird")
    code, notes = run(tmp_path, boom)
    assert code == 2 and "failed" in notes.sent[0] and "weird" in notes.sent[0]


def test_preview_export_never_touches_the_live_index(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(preview.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(preview, "_start_and_wait", lambda fe, port: "http://127.0.0.1:3001")
    preview.build("nav/run-x", tmp_path / "work")
    git_cmds = [c for c in calls if c and c[0] == "git"]
    assert git_cmds and all(c[1] not in ("reset", "checkout", "add") for c in git_cmds)
    assert any(c[1] == "archive" for c in git_cmds)


# ---- 2026-11-02: wait for the live rebuild instead of reporting the old build as faults ----

def test_live_run_skipped_when_console_not_rebuilt(tmp_path):
    browsed = []
    code, notes = run(tmp_path, lambda base, out: browsed.append(base) or [snap("/trader", scroll=900)],
                      wait_fn=lambda: "the live console hasn't been rebuilt since the last frontend merge (waited 15 min)")
    assert code == 0 and browsed == []
    assert notes.sent == ["Site checker: skipped — the live console hasn't been rebuilt since the last "
                          "frontend merge (waited 15 min)"]
    assert not (tmp_path / "store.json").exists()  # nothing recorded, nothing cleared


def test_live_run_waits_then_checks(tmp_path):
    calls = []
    code, notes = run(tmp_path, lambda base, out: [snap("/")], wait_fn=lambda: calls.append(1))
    assert code == 0 and calls == [1]


def test_other_base_never_waits(tmp_path):
    def boom():
        raise AssertionError("waited for a non-live base")
    code, _ = run(tmp_path, lambda base, out: [snap("/")], "--base", "http://127.0.0.1:3001", wait_fn=boom)
    assert code == 0
