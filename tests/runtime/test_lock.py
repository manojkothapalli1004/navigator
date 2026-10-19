import threading
import time

import pytest
from navigator.runtime.lock import SingleFlightLock, file_lock

def test_second_acquire_fails(tmp_path):
    lock = SingleFlightLock(tmp_path/"nav.lock")
    with lock.acquire():
        with pytest.raises(RuntimeError):
            with SingleFlightLock(tmp_path/"nav.lock").acquire():
                pass

def test_reacquire_after_release(tmp_path):
    p = tmp_path/"nav.lock"
    with SingleFlightLock(p).acquire():
        pass
    with SingleFlightLock(p).acquire():
        pass  # no error

def test_stale_lock_from_dead_pid_is_recovered(tmp_path):
    p = tmp_path/"nav.lock"
    p.write_text("999999999")  # PID far above pid_max: guaranteed dead
    with SingleFlightLock(p).acquire():
        pass  # stale lock reclaimed, no error
    assert not p.exists()

def test_lock_held_by_live_pid_still_blocks(tmp_path):
    import os
    p = tmp_path/"nav.lock"
    p.write_text(str(os.getpid()))  # our own PID is definitely alive
    with pytest.raises(RuntimeError, match="single-flight lock held"):
        with SingleFlightLock(p).acquire():
            pass
    assert p.exists()  # a live holder's lock must not be deleted

def test_unparseable_lock_content_is_treated_as_stale(tmp_path):
    p = tmp_path/"nav.lock"
    p.write_text("not-a-pid")
    with SingleFlightLock(p).acquire():
        pass  # unverifiable holder: reclaim
    assert not p.exists()


def test_is_held_reports_a_live_holder_only(tmp_path):
    p = tmp_path / "nav.lock"
    assert SingleFlightLock(p).is_held() is False
    with SingleFlightLock(p).acquire():
        assert SingleFlightLock(p).is_held() is True
    assert SingleFlightLock(p).is_held() is False
    p.write_text("999999999")  # stale: dead holder
    assert SingleFlightLock(p).is_held() is False


def test_file_lock_blocks_a_second_holder_until_release(tmp_path):
    lock = tmp_path / "locks" / "triage.json.lock"  # the parent directory is created
    order, entered, release = [], threading.Event(), threading.Event()

    def holder():
        with file_lock(lock):
            entered.set()
            release.wait(5)
            order.append("holder-out")

    def waiter():
        entered.wait(5)
        with file_lock(lock):
            order.append("waiter-in")

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    assert entered.wait(5)
    time.sleep(0.2)
    assert order == []  # the waiter is blocked, not refused
    release.set()
    for t in threads:
        t.join(5)
    assert order == ["holder-out", "waiter-in"]
    assert lock.exists()  # the file stays; only the flock is released


def test_file_lock_can_be_taken_again_after_release(tmp_path):
    with file_lock(tmp_path / "a.lock"):
        pass
    with file_lock(tmp_path / "a.lock"):
        pass  # no error, no deadlock
