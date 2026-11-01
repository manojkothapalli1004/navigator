import os
import stat

import pytest

from navigator.safety.command_runner import FRONTEND_SUITE, CommandRunner
from navigator.safety.errors import CommandRejected


def _script(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


def test_frontend_suite_order():
    assert FRONTEND_SUITE == (("node_modules/.bin/tsc", "--noEmit"),
                              ("node_modules/.bin/vitest", "run"),
                              ("node_modules/.bin/next", "build"))


@pytest.mark.parametrize("argv", [["node_modules/.bin/vitest"], ["rm", "-rf", "x"],
                                  ["node_modules/.bin/vitest", "run", "--watch"]])
def test_run_fixed_rejects_unlisted(tmp_path, argv):
    with pytest.raises(CommandRejected):
        CommandRunner([], tmp_path).run_fixed(argv, cwd=tmp_path, timeout_s=5)


def test_run_fixed_rejects_cwd_outside_root(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    with pytest.raises(CommandRejected):
        CommandRunner([], root).run_fixed(["node_modules/.bin/vitest", "run"], cwd=tmp_path, timeout_s=5)


def test_run_fixed_runs_listed(tmp_path):
    _script(tmp_path, "node_modules/.bin/vitest", "echo ok")
    r = CommandRunner([], tmp_path).run_fixed(["node_modules/.bin/vitest", "run"], cwd=tmp_path, timeout_s=5)
    assert (r.returncode, r.stdout.strip()) == (0, "ok")


def test_run_fixed_passes_env(tmp_path):
    _script(tmp_path, "node_modules/.bin/tsc", 'echo "$HTTPS_PROXY"')
    r = CommandRunner([], tmp_path).run_fixed(["node_modules/.bin/tsc", "--noEmit"], cwd=tmp_path,
                                             timeout_s=5, env={"HTTPS_PROXY": "http://127.0.0.1:9",
                                                               "PATH": os.environ["PATH"]})
    assert r.stdout.strip() == "http://127.0.0.1:9"


def test_run_fixed_timeout_rejected(tmp_path):
    _script(tmp_path, "node_modules/.bin/next", "sleep 5")
    with pytest.raises(CommandRejected, match="timed out"):
        CommandRunner([], tmp_path).run_fixed(["node_modules/.bin/next", "build"], cwd=tmp_path, timeout_s=0.3)
