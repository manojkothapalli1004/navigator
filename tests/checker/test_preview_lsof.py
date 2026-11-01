"""Trial run-01M6BYVF6HJS0ZX74A6T8HSXDC: the sealed verify PATH has no /usr/sbin, so
`lsof` by bare name raised FileNotFoundError and the re-check could not stop the preview."""
from navigator.checker import preview


def test_stop_runs_lsof_by_absolute_path(monkeypatch):
    seen = []
    class R:
        stdout = ""
    monkeypatch.setattr(preview.subprocess, "run", lambda argv, **kw: seen.append(argv) or R())
    monkeypatch.setenv("PATH", "/nonexistent")
    preview.stop(3001)
    assert seen[0][0] == "/usr/sbin/lsof"
