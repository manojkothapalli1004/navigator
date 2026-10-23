"""Quota pause cap + unpause (rev2): every pause is capped at 24h, the cap on a file
pause is anchored to the file's mtime (it never slides forward), and removing the
shared pause file unpauses a running daemon."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.clients.orc_client import FakeORCClient
from navigator.config import DEFAULT_ALLOWLIST, Settings
from navigator.contracts.enums import Role
from navigator.runtime.drivers.base import DriverResult
from navigator.runtime.loop import (Navigator, QUOTA_MAX_PAUSE_S, read_quota_pause,
                                    write_quota_pause)
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import QuotaExhaustedError


class CountingDriver:
    def __init__(self, raise_with=None):
        self.raise_with = raise_with
        self.calls = 0

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


def _nav(tmp_path, driver, pause_path=None):
    repo = _repo(tmp_path)
    return Navigator(
        act=FakeACTClient(), orc=FakeORCClient(), driver=driver,
        runner=CommandRunner(allowlist=DEFAULT_ALLOWLIST, worktree_root=tmp_path / "wt"),
        settings=Settings(mode="dry_run",
                          quota_pause_path=str(pause_path) if pause_path else None,
                          outcome_path=str(tmp_path / "o.jsonl")),
        repo=str(repo), lock_path=str(tmp_path / "lock"))


def _write(path, until):
    path.write_text(json.dumps({"until": until.isoformat(timespec="seconds")}),
                    encoding="utf-8")


def _quota(now, delta):
    return QuotaExhaustedError("x", marker="session limit",
                               resets_at=(now + delta).isoformat())


def _limit(now):
    return now + timedelta(seconds=QUOTA_MAX_PAUSE_S) + timedelta(seconds=5)


def test_far_future_file_cap_does_not_slide(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    _write(path, now + timedelta(days=30))
    nav = _nav(tmp_path, CountingDriver(), pause_path=path)
    assert nav._quota_paused(now) is True
    first = nav.quota_paused_until
    assert first <= _limit(now)
    # an hour later the cap must not have moved forward
    assert nav._quota_paused(now + timedelta(hours=1)) is True
    assert nav.quota_paused_until == first
    # a fresh process sees the same cap
    (tmp_path / "n2").mkdir()
    nav2 = _nav(tmp_path / "n2", CountingDriver(), pause_path=path)
    assert nav2._quota_paused(now + timedelta(hours=2)) is True
    assert nav2.quota_paused_until == first
    assert read_quota_pause(path) <= now + timedelta(hours=24, seconds=5)


def test_read_quota_pause_caps_by_file_mtime(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    _write(path, now + timedelta(days=30))
    t = (now - timedelta(hours=23)).timestamp()
    os.utime(path, (t, t))
    got = read_quota_pause(path)
    assert abs((got - (now + timedelta(hours=1))).total_seconds()) <= 5
    t = (now - timedelta(hours=25)).timestamp()
    os.utime(path, (t, t))
    assert read_quota_pause(path) <= now
    nav = _nav(tmp_path, CountingDriver(), pause_path=path)
    assert nav._quota_paused(now) is False


def test_garbage_file_never_crashes(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    path.write_text("{not json", encoding="utf-8")
    nav = _nav(tmp_path, CountingDriver(), pause_path=path)
    assert nav._quota_paused(now) is False
    _write(path, now + timedelta(hours=1))
    assert nav._quota_paused(now) is True
    path.write_text("{not json", encoding="utf-8")
    assert nav._quota_paused(now) is False
    assert nav.quota_paused_until is None


def test_far_future_resets_at_is_capped(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    nav = _nav(tmp_path, CountingDriver(raise_with=_quota(now, timedelta(days=10))),
               pause_path=path)
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert read_quota_pause(path) <= _limit(now)
    assert nav.quota_paused_until <= _limit(now)


def test_removing_file_unpauses_adopted_pause(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    _write(path, now + timedelta(hours=1))
    driver = CountingDriver()
    nav = _nav(tmp_path, driver, pause_path=path)
    assert nav._quota_paused(now) is True
    path.unlink()
    assert nav._quota_paused(now) is False
    assert nav.quota_paused_until is None
    nav.periodic_tick()
    assert driver.calls == 1


def test_removing_file_unpauses_recorded_pause(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "q.json"
    driver = CountingDriver(raise_with=_quota(now, timedelta(hours=1)))
    nav = _nav(tmp_path, driver, pause_path=path)
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert driver.calls == 1
    assert path.exists()
    path.unlink()
    driver.raise_with = None
    nav.periodic_tick()
    assert driver.calls == 2


def test_path_unset_pause_not_cleared(tmp_path):
    now = datetime.now(timezone.utc)
    nav = _nav(tmp_path, CountingDriver(raise_with=_quota(now, timedelta(hours=1))),
               pause_path=None)
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert nav._quota_paused(now) is True
    assert nav._quota_paused(now) is True


def test_write_failure_pause_kept(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "blocker").write_text("not a directory", encoding="utf-8")
    path = tmp_path / "blocker" / "q.json"
    nav = _nav(tmp_path, CountingDriver(raise_with=_quota(now, timedelta(hours=1))),
               pause_path=path)
    with pytest.raises(QuotaExhaustedError):
        nav.periodic_tick()
    assert not path.exists()
    assert nav._quota_paused(now) is True
    assert nav.quota_paused_until is not None


def test_write_quota_pause_returns_bool(tmp_path):
    now = datetime.now(timezone.utc)
    assert write_quota_pause(None, now + timedelta(hours=1)) is False
    assert write_quota_pause("", now + timedelta(hours=1)) is False
    path = tmp_path / "q.json"
    assert write_quota_pause(str(path), now + timedelta(hours=2)) is True
    # an earlier time never shortens the pause on file, but the file still covers it
    assert write_quota_pause(str(path), now + timedelta(hours=1)) is True
    got = read_quota_pause(path)
    assert got >= now + timedelta(hours=2) - timedelta(seconds=1)
    assert got <= now + timedelta(hours=2)
    (tmp_path / "blocker").write_text("not a directory", encoding="utf-8")
    assert write_quota_pause(str(tmp_path / "blocker" / "q.json"),
                             now + timedelta(hours=1)) is False
