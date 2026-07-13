import os
from contextlib import contextmanager
from pathlib import Path

class SingleFlightLock:
    def __init__(self, path: Path):
        self.path = Path(path)

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
