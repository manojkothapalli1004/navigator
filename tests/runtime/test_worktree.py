import subprocess
from pathlib import Path
from navigator.runtime.worktree import create_worktree, preserve_failed, remove_worktree

def _seed_repo(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","-c","user.email=t@t","-c","user.name=t","commit","--allow-empty","-m","base"],
                   cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha

def test_create_and_remove_worktree(tmp_path):
    repo, sha = _seed_repo(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-1",
                        worktree_root=tmp_path/"wt")
    assert Path(h.path).exists() and h.branch == "nav/run-1"
    remove_worktree(h)
    assert not Path(h.path).exists()

def test_preserve_failed_keeps_dir(tmp_path):
    repo, sha = _seed_repo(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-2",
                        worktree_root=tmp_path/"wt")
    preserve_failed(h)
    assert h.preserved is True and Path(h.path).exists()
