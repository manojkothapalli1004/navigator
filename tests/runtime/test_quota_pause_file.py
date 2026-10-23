"""The quota pause file shared by the daemon and once.sh: the planner (periodic_tick)
and approval_poll both honour it, and a planner session limit records it."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.config import DEFAULT_ALLOWLIST, Settings
from navigator.contracts.enums import ProposalStatus, Role
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.loop import Navigator, read_quota_pause, write_quota_pause
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import QuotaExhaustedError


class CountingDriver:
    def __init__(self, raise_with=None):
        self.calls = 0
        self.raise_with = raise_with

    def run_role(self, request):
        self.calls += 1
        if self.raise_with is not None:
            raise self.raise_with
        return DriverResult(role=Role.planner, proposals=[])


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "--allow-empty", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


def _nav(tmp_path, driver, *, mode="dry_run", pause_path=None):
    repo = _repo(tmp_path)
    settings = Settings(mode=mode, quota_pause_path=pause_path,
                        outcome_path=str(tmp_path / "o.jsonl"))
    return Navigator(act=FakeACTClient(), orc=FakeORCClient(), driver=driver,
                     runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST,
                                          worktree_root=tmp_path / "wt"),
                     settings=settings, repo=str(repo), lock_path=str(tmp_path / "lock"))


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _write(p, until):
    p.write_text(json.dumps({"until": until.isoformat(timespec="seconds")}))


def _files(root):
    return {str(f.relative_to(root)) for f in root.rglob("*")
            if f.is_file() and ".git" not in f.relative_to(root).parts}


# a) read_quota_pause

def test_read_quota_pause_returns_none_for_unset_missing_or_bad_input(tmp_path):
    p = tmp_path / "pause.json"
    assert read_quota_pause(None) is None
    assert read_quota_pause("") is None
    assert read_quota_pause(p) is None  # missing
    p.write_text("not json at all {")
    assert read_quota_pause(p) is None
    p.write_text("{}")
    assert read_quota_pause(p) is None
    p.write_text(json.dumps({"until": "nope"}))
    assert read_quota_pause(p) is None
    p.write_text(json.dumps(["until"]))
    assert read_quota_pause(p) is None


def test_read_quota_pause_treats_naive_iso_as_utc(tmp_path):
    p = tmp_path / "pause.json"
    p.write_text(json.dumps({"until": "2026-10-23T10:00:00"}))
    got = read_quota_pause(str(p))
    assert got == datetime(2026, 10, 23, 10, 0, 0, tzinfo=timezone.utc)
    assert got.tzinfo is not None


# b) write_quota_pause never shortens

def test_write_quota_pause_never_shortens(tmp_path):
    p = tmp_path / "deep" / "dir" / "pause.json"  # parents are created
    t = _now()
    write_quota_pause(str(p), t + timedelta(hours=2))
    assert read_quota_pause(p) == t + timedelta(hours=2)
    write_quota_pause(str(p), t + timedelta(hours=1))
    assert read_quota_pause(p) == t + timedelta(hours=2)
    write_quota_pause(str(p), t + timedelta(hours=3))
    assert read_quota_pause(p) == t + timedelta(hours=3)
    assert json.loads(p.read_text()) == {
        "until": (t + timedelta(hours=3)).isoformat(timespec="seconds")}
    assert [f for f in p.parent.iterdir() if f.suffix == ".tmp"] == []


def test_write_quota_pause_without_path_is_a_noop(tmp_path):
    before = _files(tmp_path)
    write_quota_pause(None, _now() + timedelta(hours=1))
    write_quota_pause("", _now() + timedelta(hours=1))
    assert _files(tmp_path) == before


# c) / d) periodic_tick honours the file

def test_periodic_tick_skips_planner_while_file_pause_is_in_future(tmp_path):
    p = tmp_path / "pause.json"
    until = _now() + timedelta(hours=1)
    _write(p, until)
    driver = CountingDriver()
    nav = _nav(tmp_path, driver, pause_path=str(p))
    assert nav.periodic_tick() == []
    assert driver.calls == 0
    assert nav.quota_paused_until == until


def test_periodic_tick_plans_once_file_pause_has_expired(tmp_path):
    p = tmp_path / "pause.json"
    _write(p, _now() - timedelta(minutes=1))
    driver = CountingDriver()
    nav = _nav(tmp_path, driver, pause_path=str(p))
    nav.periodic_tick()
    assert driver.calls == 1


# e) a planner session limit records the pause

def test_planner_quota_error_propagates_and_records_pause_in_file(tmp_path):
    p = tmp_path / "pause.json"
    resets = _now() + timedelta(minutes=30)
    err = QuotaExhaustedError("x", marker="session limit",
                              resets_at=resets.isoformat(timespec="seconds"))
    driver = CountingDriver(raise_with=err)
    nav = _nav(tmp_path, driver, pause_path=str(p))
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert driver.calls == 1
    assert read_quota_pause(p) == resets
    assert nav.quota_paused_until == resets
    # the next tick honours the pause this process just wrote
    assert nav.periodic_tick() == []
    assert driver.calls == 1


# f) approval_poll honours the file

def test_approval_poll_honours_file_pause(tmp_path):
    p = tmp_path / "pause.json"
    _write(p, _now() + timedelta(hours=1))
    driver = CountingDriver()
    nav = _nav(tmp_path, driver, mode="execute", pause_path=str(p))
    prop = nav.act.create_proposal(Proposal(
        origin="periodic",
        proposal_origin_hash=compute_origin_hash(nav.repo, "x", ["a.py"], "periodic", None),
        title="t", rationale="r", repo=nav.repo, base_commit_sha="abc",
        intended_changes="x", affected_files=["a.py"]))
    nav.act.set_proposal_status(prop.id, ProposalStatus.approved)
    assert nav.approval_poll() == []
    assert nav.act.list_runs_for_proposal(prop.id) == []
    assert driver.calls == 0
    assert nav.quota_paused_until is not None


# g) path unset = today's behaviour

def test_without_pause_path_tick_plans_and_touches_no_file(tmp_path):
    driver = CountingDriver()
    nav = _nav(tmp_path, driver, pause_path=None)
    before = _files(tmp_path)
    nav.periodic_tick()
    assert driver.calls == 1
    assert _files(tmp_path) == before
    assert not any("quota" in f or "pause" in f for f in _files(tmp_path))


def test_without_pause_path_planner_quota_error_creates_no_file(tmp_path):
    err = QuotaExhaustedError("x", marker="session limit",
                              resets_at=(_now() + timedelta(minutes=30)).isoformat())
    nav = _nav(tmp_path, CountingDriver(raise_with=err), pause_path=None)
    before = _files(tmp_path)
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert nav.quota_paused_until is not None
    assert _files(tmp_path) == before


# h) Settings.from_env

def test_settings_from_env_reads_quota_pause_path(monkeypatch):
    monkeypatch.delenv("NAVIGATOR_REPOS_FILE", raising=False)
    monkeypatch.setenv("NAVIGATOR_QUOTA_PAUSE_PATH", "/tmp/navigator-quota-pause.json")
    assert Settings.from_env().quota_pause_path == "/tmp/navigator-quota-pause.json"
    monkeypatch.setenv("NAVIGATOR_QUOTA_PAUSE_PATH", "")
    assert Settings.from_env().quota_pause_path is None
    monkeypatch.delenv("NAVIGATOR_QUOTA_PAUSE_PATH", raising=False)
    assert Settings.from_env().quota_pause_path is None
    assert Settings().quota_pause_path is None
