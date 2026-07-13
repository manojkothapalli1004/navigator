import pytest
from navigator.runtime.lock import SingleFlightLock

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
