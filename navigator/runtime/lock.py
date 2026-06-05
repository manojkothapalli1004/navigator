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
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            yield
        finally:
            try: self.path.unlink()
            except FileNotFoundError: pass
