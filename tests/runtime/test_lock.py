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
