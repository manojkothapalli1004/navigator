import subprocess
from pathlib import Path
from navigator.runtime.worktree import create_worktree
from navigator.runtime.checkpoint import checkpoint, restore, diff_artifact

def _seed(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    subprocess.run(["git","init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True)
    subprocess.run(["git","config","user.name","t"], cwd=repo, check=True)
    subprocess.run(["git","commit","--allow-empty","-m","base"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha

def test_diff_artifact_raises_on_bad_sha(tmp_path):
    import pytest
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-bad", worktree_root=tmp_path/"wt")
    with pytest.raises(subprocess.CalledProcessError):
        diff_artifact(h.path, base_commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

def test_checkpoint_restore_roundtrip(tmp_path):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-1", worktree_root=tmp_path/"wt")
    (h.path/"f.txt").write_text("v1")
    cp = checkpoint(h.path, run_id="run-1", n=1, label="pre-edit")
    msg = subprocess.run(["git","log","-1","--pretty=%s"], cwd=h.path, capture_output=True, text=True).stdout.strip()
    assert msg == "navigator-checkpoint:run-1:1"
    (h.path/"f.txt").write_text("v2-broken")
    restore(h.path, cp)
    assert (h.path/"f.txt").read_text() == "v1"

def test_diff_artifact_shows_changes(tmp_path):
    repo, sha = _seed(tmp_path)
    h = create_worktree(repo=repo, base_commit_sha=sha, run_id="run-2", worktree_root=tmp_path/"wt")
    (h.path/"new.py").write_text("print(1)\n")
    checkpoint(h.path, run_id="run-2", n=1, label="add")
    d = diff_artifact(h.path, base_commit_sha=sha)
    assert "new.py" in d
