import os
from dataclasses import dataclass, field

DEFAULT_ALLOWLIST = [
    "git status", "git diff", "git log",
    "pytest", "python3 -m py_compile", "uv run pytest",
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
    max_open_proposals: int = 10
    memory_chars_per_section: int = 3000
    max_failed_tests: int = 50
    repo: str | None = None
    lock_path: str = "/tmp/navigator.lock"
    outcome_path: str = "/tmp/navigator-outcomes.jsonl"
    sdk_model: str | None = None
    sdk_timeout_s: float = 300.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            driver=os.getenv("NAVIGATOR_DRIVER", "fake"),
            mode=os.getenv("NAVIGATOR_MODE", "dry_run"),
            act_base_url=os.getenv("NAVIGATOR_ACT_BASE_URL"),
            orc_base_url=os.getenv("NAVIGATOR_ORC_BASE_URL"),
            worktree_root=os.getenv("NAVIGATOR_WORKTREE_ROOT", "/tmp/navigator-worktrees"),
            tick_interval_s=_int_env("NAVIGATOR_TICK_INTERVAL_S", "900"),
            allowed_commands=_allowlist_env("NAVIGATOR_ALLOWED_COMMANDS"),
            max_tool_failures=_positive_int_env("NAVIGATOR_MAX_TOOL_FAILURES", "5"),
            max_identical_failures=_int_env("NAVIGATOR_MAX_IDENTICAL_FAILURES", "3"),
            max_run_duration_s=_int_env("NAVIGATOR_MAX_RUN_DURATION_S", "1800"),
            max_cost_usd=_positive_float_env("NAVIGATOR_MAX_COST_USD", "2.0"),
            max_proposals_per_tick=_int_env("NAVIGATOR_MAX_PROPOSALS_PER_TICK", "3"),
            max_open_proposals=_positive_int_env("NAVIGATOR_MAX_OPEN_PROPOSALS", "10"),
            memory_chars_per_section=_positive_int_env("NAVIGATOR_MEMORY_CHARS_PER_SECTION", "3000"),
            max_failed_tests=_int_env("NAVIGATOR_MAX_FAILED_TESTS", "50"),
            repo=os.getenv("NAVIGATOR_REPO"),
            lock_path=os.getenv("NAVIGATOR_LOCK_PATH", "/tmp/navigator.lock"),
            outcome_path=os.getenv("NAVIGATOR_OUTCOME_PATH", "/tmp/navigator-outcomes.jsonl"),
            sdk_model=os.getenv("NAVIGATOR_SDK_MODEL"),
            sdk_timeout_s=_positive_float_env("NAVIGATOR_SDK_TIMEOUT_S", "300.0"),
        )


def _int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


def _float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a number, got {raw!r}") from e


def _positive_int_env(name: str, default: str) -> int:
    v = _int_env(name, default)
    if v < 1:
        raise ValueError(f"{name} must be >= 1, got {v}")
    return v


def _positive_float_env(name: str, default: str) -> float:
    v = _float_env(name, default)
    if v <= 0:
        raise ValueError(f"{name} must be > 0, got {v}")
    return v


_ALLOWLIST_FORBIDDEN = set(";|&$`><\n*?!\\/")

def _allowlist_env(name: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(DEFAULT_ALLOWLIST)
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    for e in entries:
        if any(ch in _ALLOWLIST_FORBIDDEN for ch in e):
            raise ValueError(f"{name} entry contains forbidden character: {e!r}")
    return entries
