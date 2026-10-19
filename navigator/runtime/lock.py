import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

class SingleFlightLock:
    def __init__(self, path: Path):
        self.path = Path(path)

    def is_held(self) -> bool:
        """True when the lock file names a live process (a stale file does not count)."""
        return self.path.exists() and self._holder_is_alive()

    def _holder_is_alive(self) -> bool:
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return False  # vanished or unverifiable holder: treat as stale
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user

    @contextmanager
    def acquire(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._holder_is_alive():
                raise RuntimeError(f"single-flight lock held: {self.path}")
            try: self.path.unlink()  # stale: holder is dead
            except FileNotFoundError: pass
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise RuntimeError(f"single-flight lock held: {self.path}")
        try:
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)  # always close, even if write raises (no fd leak)
            yield
        finally:
            try: self.path.unlink()
            except FileNotFoundError: pass


@contextmanager
def file_lock(path):
    """Blocking advisory lock (flock) on `path`, created if missing. Unlike
    SingleFlightLock it waits instead of refusing: it serializes short read-modify-write
    sections (triage.json) between the daemon and the Telegram reader. The lock file is
    left in place; a holder that dies releases it automatically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
