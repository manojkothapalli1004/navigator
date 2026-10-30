"""Merge tiers 3a3: tier A is a whitelist; more agent-instruction and dependency names are C."""
from types import SimpleNamespace

import pytest

from navigator.runtime import merge_tiers
from navigator.runtime.merge_tiers import classify, changed_files_for_run


@pytest.mark.parametrize("path", [
    "tests/pytest.ini", "tests/plugins/x.py", "tests/__init__.py", "tests/sitecustomize.py", "tests/setup.cfg",
    "tests/helpers.py", "backend/tests/helpers.py", "tests/tox.toml",
])
def test_non_test_files_in_tests_dirs_are_b(path):
    # NEGATIVE: every case was A on main
    assert classify([path], repo_name="waker") == "B"
    assert classify(["README.md", path], repo_name="waker") == "B"


def test_test_files_and_fixtures_stay_a():
    assert classify(["tests/test_x.py", "tests/x_test.py", "web/tests/a.test.ts", "web/tests/b.spec.tsx",
                     "tests/fixtures/x.json", "tests/data/x.csv", "tests/snap/x.snap", "tests/img/x.png",
                     "README.md", "docs/a.rst", "docs/img/x.svg", "notes.txt"], repo_name="waker") == "A"


@pytest.mark.parametrize("path", ["docs/Makefile", "docs/index.html", "docs/x.ipynb", "docs/script.js"])
def test_docs_dir_non_doc_files_are_b(path):
    # NEGATIVE: first three were A on main
    assert classify([path], repo_name="waker") == "B"


@pytest.mark.parametrize("path", [
    "GEMINI.md", "docs/gemini.md", "CLAUDE.local.md", "sub/.claude/settings.json", "app/.claude/commands/x.md",
    "constraints.txt", "constraints-dev.txt", "runtime.txt", "docs/.envrc", ".envrc", "tests/x.pth", "x.pth",
])
def test_new_c_names(path):
    assert classify([path], repo_name="waker") == "C"


def test_non_utf8_name_is_none(tmp_path, monkeypatch):
    # NEGATIVE: raised on main
    def fake(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    monkeypatch.setattr(merge_tiers.subprocess, "run", fake)
    assert changed_files_for_run(str(tmp_path), SimpleNamespace(base_commit_sha="a", verified_sha="b")) is None
