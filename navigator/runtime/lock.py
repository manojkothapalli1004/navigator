"""Single-flight and advisory file locks. The single-flight lock file holds
"<pid> <boot>" (boot = this boot's epoch seconds from `sysctl kern.boottime`, when
known): a lock recorded in an earlier boot is stale even when its PID has since been
reused by an unrelated live process, so a reboot never needs a manual delete."""
import fcntl
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

_BOOT_TIME: int | None = None  # cached after the first successful read; None is never cached
# launchd services often run with a PATH lacking /usr/sbin, where macOS keeps sysctl; use the
# absolute path when it exists so the reboot-safe lock works there too, else fall back to PATH
_SYSCTL = "/usr/sbin/sysctl"


def boot_time() -> int | None:
    """Epoch seconds of the current boot (macOS `sysctl -n kern.boottime`), or None when
    unknown (command missing, non-zero exit, timeout, unparseable output)."""
    global _BOOT_TIME
    if _BOOT_TIME is not None:
        return _BOOT_TIME
    try:
        cmd = _SYSCTL if os.path.exists(_SYSCTL) else "sysctl"
        proc = subprocess.run([cmd, "-n", "kern.boottime"], capture_output=True,
                              text=True, timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"sec\s*=\s*(\d+)", proc.stdout or "")
    if not m:
        return None
    _BOOT_TIME = int(m.group(1))
    return _BOOT_TIME


class SingleFlightLock:
    def __init__(self, path: Path):
        self.path = Path(path)

    def is_held(self) -> bool:
        """True when the lock file names a live process (a stale file does not count)."""
        return self.path.exists() and self._holder_is_alive()

    def _holder_is_alive(self) -> bool:
        try:
            parts = self.path.read_text().split()
            pid = int(parts[0])
        except (OSError, ValueError, IndexError):
            return False  # vanished or unverifiable holder: treat as stale
        recorded = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        if recorded is not None:
            current = boot_time()
            if current is not None and recorded != current:
                return False  # written in an earlier boot: the PID is a reused one
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user

    @contextmanager
    def acquire(self):
        # read the boot time BEFORE creating the file: sysctl may take up to 5 s and an
        # empty lock file in that window would be read as stale by a second process
        b = boot_time()
        content = f"{os.getpid()} {b}" if b is not None else str(os.getpid())
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
                os.write(fd, content.encode())
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
