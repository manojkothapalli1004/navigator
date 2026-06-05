import os
from dataclasses import dataclass, field

DEFAULT_ALLOWLIST = [
    "git status", "git diff", "git log",
    "pytest", "python -m py_compile", "uv run pytest",
]

@dataclass
class Settings:
    driver: str = "fake"
    mode: str = "dry_run"
    act_base_url: str | None = None
    orc_base_url: str | None = None
    worktree_root: str = "/tmp/navigator-worktrees"
    tick_interval_s: int = 900
    allowed_commands: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWLIST))
    max_tool_failures: int = 5
    max_identical_failures: int = 3
    max_run_duration_s: int = 1800
    max_cost_usd: float = 2.0
    max_proposals_per_tick: int = 3
    max_failed_tests: int = 50

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            driver=os.getenv("NAVIGATOR_DRIVER", "fake"),
            mode=os.getenv("NAVIGATOR_MODE", "dry_run"),
            act_base_url=os.getenv("NAVIGATOR_ACT_BASE_URL"),
            orc_base_url=os.getenv("NAVIGATOR_ORC_BASE_URL"),
            worktree_root=os.getenv("NAVIGATOR_WORKTREE_ROOT", "/tmp/navigator-worktrees"),
            tick_interval_s=int(os.getenv("NAVIGATOR_TICK_INTERVAL_S", "900")),
        )
