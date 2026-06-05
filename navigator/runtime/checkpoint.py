import subprocess
from pathlib import Path

def checkpoint(worktree_path: Path, run_id: str, n: int, label: str) -> str:
    wt = Path(worktree_path)
    subprocess.run(["git","add","-A"], cwd=wt, check=True, capture_output=True)
    msg = f"navigator-checkpoint:{run_id}:{n}"
    subprocess.run(["git","-c","user.email=navigator@local","-c","user.name=navigator",
                    "commit","--allow-empty","-m",msg], cwd=wt, check=True, capture_output=True)
    return subprocess.run(["git","rev-parse","HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()

def restore(worktree_path: Path, checkpoint_sha: str) -> None:
    subprocess.run(["git","reset","--hard",checkpoint_sha],
                   cwd=Path(worktree_path), check=True, capture_output=True)

def diff_artifact(worktree_path: Path, base_commit_sha: str) -> str:
    return subprocess.run(["git","diff",f"{base_commit_sha}..HEAD"],
                          cwd=Path(worktree_path), capture_output=True, text=True).stdout
