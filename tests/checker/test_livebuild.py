"""2026-11-02: the checker must not scan a console that hasn't been rebuilt since a frontend merge
(42 false faults on 11-01 when the WatchPaths run raced the rebuild)."""
import os
import subprocess

from navigator.checker import livebuild


def _git(repo, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _repo(tmp_path, t0):
    repo = tmp_path / "act"
    (repo / "frontend" / ".next").mkdir(parents=True)
    (repo / "backend").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "frontend" / "a.tsx").write_text("1")
    (repo / "backend" / "b.py").write_text("1")
    (repo / ".gitignore").write_text(".next/\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "init", when=t0)
    return repo


def _built_at(repo, t):
    bid = repo / "frontend" / ".next" / "BUILD_ID"
    bid.write_text("x")
    os.utime(bid, (t, t))


def test_backend_only_change_after_build_is_not_stale(tmp_path):
    repo = _repo(tmp_path, 1_000_000)
    _built_at(repo, 1_000_100)
    (repo / "backend" / "b.py").write_text("2")
    _git(repo, "commit", "-qam", "backend", when=1_000_200)
    assert livebuild.frontend_changed_since_build(repo) is False


def test_frontend_commit_after_build_is_stale(tmp_path):
    repo = _repo(tmp_path, 1_000_000)
    _built_at(repo, 1_000_100)
    (repo / "frontend" / "a.tsx").write_text("2")
    _git(repo, "commit", "-qam", "frontend", when=1_000_200)
    assert livebuild.frontend_changed_since_build(repo) is True


def test_frontend_branch_merged_after_build_is_stale(tmp_path):
    # the branch commit is OLDER than the build; only the merge commit is newer (first-parent view)
    repo = _repo(tmp_path, 1_000_000)
    _git(repo, "checkout", "-qb", "feat")
    (repo / "frontend" / "a.tsx").write_text("3")
    _git(repo, "commit", "-qam", "feat", when=1_000_050)
    _git(repo, "checkout", "-q", "main")
    _built_at(repo, 1_000_100)
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "merge feat", when=1_000_200)
    assert livebuild.frontend_changed_since_build(repo) is True


def test_build_newer_than_everything_is_fresh(tmp_path):
    repo = _repo(tmp_path, 1_000_000)
    _built_at(repo, 1_000_500)
    assert livebuild.frontend_changed_since_build(repo) is False


def test_no_build_at_all_counts_as_stale(tmp_path):
    repo = _repo(tmp_path, 1_000_000)
    assert livebuild.frontend_changed_since_build(repo) is True


class Clock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def sleep(self, s):
        self.t += s


def test_wait_returns_none_once_rebuilt_and_up():
    c = Clock(); states = iter([(True, True), (True, True), (False, False), (False, True)])
    state = {"cur": (True, True)}

    def tick():
        state["cur"] = next(states, (False, True))
    stale = lambda: state["cur"][0]
    up = lambda: state["cur"][1]

    def sleep(s):
        c.sleep(s); tick()
    assert livebuild.wait_for_live_build(stale_fn=stale, up_fn=up, sleep=sleep, clock=c,
                                         timeout_s=900, poll_s=20) is None
    assert c.t == 80


def test_wait_times_out_with_a_reason():
    c = Clock()
    reason = livebuild.wait_for_live_build(stale_fn=lambda: True, up_fn=lambda: True, sleep=c.sleep, clock=c,
                                           timeout_s=900, poll_s=20)
    assert reason == "the live console hasn't been rebuilt since the last frontend merge (waited 15 min)"
    assert 900 <= c.t <= 920


def test_wait_down_console_times_out_too():
    c = Clock()
    reason = livebuild.wait_for_live_build(stale_fn=lambda: False, up_fn=lambda: False, sleep=c.sleep, clock=c,
                                           timeout_s=60, poll_s=20)
    assert reason == "the live console didn't answer for 1 min"


def test_ready_at_once_never_sleeps():
    slept = []
    assert livebuild.wait_for_live_build(stale_fn=lambda: False, up_fn=lambda: True, sleep=slept.append,
                                         clock=lambda: 0.0) is None
    assert slept == []
