import os
from contextlib import contextmanager
from pathlib import Path

class SingleFlightLock:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def acquire(self):
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
