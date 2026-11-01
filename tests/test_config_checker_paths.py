import json

import pytest

from navigator.config import _repos_env


def _load(tmp_path, monkeypatch, **entry):
    f = tmp_path / "repos.json"
    f.write_text(json.dumps([dict(path=str(tmp_path), **entry)]))
    monkeypatch.setenv("NAVIGATOR_REPOS_FILE", str(f))
    return _repos_env()[0]


def test_checker_a_paths_parsed(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, checker_a_paths=["frontend/components/*"])
    assert cfg.checker_a_paths == ["frontend/components/*"]


def test_checker_a_paths_default_none(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).checker_a_paths is None


@pytest.mark.parametrize("bad", ["frontend/*", [1], ["../x/*"], ["/abs/*"]])
def test_checker_a_paths_rejects_bad_shapes(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match="checker_a_paths"):
        _load(tmp_path, monkeypatch, checker_a_paths=bad)
