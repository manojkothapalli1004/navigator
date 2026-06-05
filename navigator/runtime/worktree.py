import subprocess
from dataclasses import dataclass
from pathlib import Path

@dataclass
class WorktreeHandle:
    repo: Path
    path: Path
    branch: str
    preserved: bool = False

def create_worktree(repo: Path, base_commit_sha: str, run_id: str, worktree_root: Path) -> WorktreeHandle:
    repo = Path(repo); worktree_root = Path(worktree_root)
    worktree_root.mkdir(parents=True, exist_ok=True)
    path = worktree_root / run_id
    branch = f"nav/{run_id}"
    subprocess.run(["git","worktree","add","-b",branch,str(path),base_commit_sha],
                   cwd=repo, check=True, capture_output=True, text=True)
    return WorktreeHandle(repo=repo, path=path, branch=branch)

def remove_worktree(handle: WorktreeHandle) -> None:
    subprocess.run(["git","worktree","remove","--force",str(handle.path)],
                   cwd=handle.repo, check=True, capture_output=True, text=True)

def preserve_failed(handle: WorktreeHandle) -> WorktreeHandle:
    handle.preserved = True
    return handle
