import json

import pytest

from navigator.config import _repos_env


def _load(tmp_path, monkeypatch, **entry):
    f = tmp_path / "repos.json"
    f.write_text(json.dumps([dict(path=str(tmp_path), **entry)]))
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(f))
    return _repos_env()[0]


def test_frontend_dir_loads(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, frontend_dir="frontend").frontend_dir == "frontend"


def test_frontend_dir_default_none(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).frontend_dir is None


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../..", "", 3, "."])
def test_frontend_dir_rejects_escape(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match="frontend_dir"):
        _load(tmp_path, monkeypatch, frontend_dir=bad)
