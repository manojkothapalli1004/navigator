import json
import os
from dataclasses import dataclass, field

DEFAULT_ALLOWLIST = [
    "git status", "git diff", "git log",
    "pytest", "python3 -m py_compile", "uv run pytest",
]

# operator decision 2026-10-15: when the configured model's account runs out of credits
# (or hits a usage/session limit) the same query moves to the next model in this chain
# instead of failing the run; the failing model is skipped for the cooldown period
DEFAULT_SDK_MODEL_FALLBACKS = ["claude-opus-5-5", "claude-sonnet-5-5"]
DEFAULT_SDK_MODEL_COOLDOWN_S = 3600

@dataclass
class RepoConfig:
    path: str
    workdir: str = "."                      # where verifier commands run (e.g. "backend")
    allowed_commands: list[str] | None = None  # None = Settings.allowed_commands
    squash: bool = False                    # public repo: one squash commit per merge
    brief: str | None = None                # repo-specific planner instructions
    context_files: list[str] | None = None  # extra files (from the live checkout) shown to the planner
    min_plan_interval_hours: float | None = None
    max_proposals_per_tick: int | None = None


@dataclass
class Settings:
    driver: str = "fake"
    mode: str = "dry_run"
    # N8: apply operator merge decisions while mode == "dry_run" (builds stay off)
    merge_in_dry_run: bool = False
    # N12d: after a merge, rebase the repo's other runs parked at the merge gate onto the
    # new HEAD and re-verify them with their recorded commands (no model call)
    rebase_stale_runs: bool = True
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
    proposal_ttl_days: int = 7
    command_timeout_s: float = 60.0
    max_failed_tests: int = 50
    repo: str | None = None
    repos: list[RepoConfig] = field(default_factory=list)
    lock_path: str = "/tmp/navigator.lock"
    outcome_path: str = "/tmp/navigator-outcomes.jsonl"
    sdk_model: str | None = None
    sdk_role_models: dict[str, str] = field(default_factory=dict)
    sdk_timeout_s: float = 300.0
    # ordered model fallback chain; empty = fallback disabled
    sdk_model_fallbacks: list[str] = field(default_factory=lambda: list(DEFAULT_SDK_MODEL_FALLBACKS))
    sdk_model_cooldown_s: int = DEFAULT_SDK_MODEL_COOLDOWN_S

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            driver=os.getenv("NAVIGATOR_DRIVER", "fake"),
            mode=os.getenv("NAVIGATOR_MODE", "dry_run"),
            merge_in_dry_run=_bool_env("NAVIGATOR_MERGE_IN_DRY_RUN", "0"),
            rebase_stale_runs=_bool_env("NAVIGATOR_REBASE_STALE_RUNS", "1"),
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
            proposal_ttl_days=_positive_int_env("NAVIGATOR_PROPOSAL_TTL_DAYS", "7"),
            command_timeout_s=_positive_float_env("NAVIGATOR_COMMAND_TIMEOUT_S", "60"),
            max_failed_tests=_int_env("NAVIGATOR_MAX_FAILED_TESTS", "50"),
            repo=os.getenv("NAVIGATOR_REPO"),
            repos=_repos_env(),
            lock_path=os.getenv("NAVIGATOR_LOCK_PATH", "/tmp/navigator.lock"),
            outcome_path=os.getenv("NAVIGATOR_OUTCOME_PATH", "/tmp/navigator-outcomes.jsonl"),
            sdk_model=os.getenv("NAVIGATOR_SDK_MODEL"),
            sdk_role_models={role: os.environ[f"NAVIGATOR_SDK_MODEL_{role.upper()}"]
                             for role in ("planner", "builder", "verifier")
                             if os.getenv(f"NAVIGATOR_SDK_MODEL_{role.upper()}")},
            sdk_timeout_s=_positive_float_env("NAVIGATOR_SDK_TIMEOUT_S", "300.0"),
            sdk_model_fallbacks=_model_list_env("NAVIGATOR_SDK_MODEL_FALLBACKS",
                                                ",".join(DEFAULT_SDK_MODEL_FALLBACKS)),
            sdk_model_cooldown_s=_positive_int_env("NAVIGATOR_SDK_MODEL_COOLDOWN_S",
                                                   str(DEFAULT_SDK_MODEL_COOLDOWN_S)),
        )


def effective_repos(settings: Settings) -> list[RepoConfig]:
    """The repos the daemon actually runs: settings.repos, else NAVIGATOR_REPO, else none.

    Shared by build_navigator and `navigator doctor` so the two can never disagree.
    """
    if settings.repos:
        return list(settings.repos)
    return [RepoConfig(path=settings.repo)] if settings.repo else []


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


def _bool_env(name: str, default: str) -> bool:
    # '1'/'true' -> True; '0'/'false'/unset (or empty) -> False; anything else is an error
    raw = os.getenv(name, default)
    if raw is None:
        raw = default
    v = raw.strip().lower()
    if v in ("1", "true"):
        return True
    if v in ("0", "false", ""):
        return False
    raise ValueError(f"{name} must be 1/true or 0/false, got {raw!r}")


def _model_list_env(name: str, default: str) -> list[str]:
    # comma-separated model ids; blanks dropped; an empty value disables fallback entirely
    raw = os.getenv(name, default)
    if raw is None:
        raw = default
    return [e.strip() for e in raw.split(",") if e.strip()]


_ALLOWLIST_FORBIDDEN = set(";|&$`><\n*?!\\/")


def _check_allowlist(name: str, entries: list[str]) -> list[str]:
    for e in entries:
        if any(ch in _ALLOWLIST_FORBIDDEN for ch in e):
            raise ValueError(f"{name} entry contains forbidden character: {e!r}")
    return entries


def _repos_env() -> list[RepoConfig]:
    path = os.getenv("NAVIGATOR_REPOS_FILE")
    if not path:
        single = os.getenv("NAVIGATOR_REPO")
        return [RepoConfig(path=single)] if single else []
    repos = []
    for entry in json.loads(open(path).read()):
        workdir = entry.get("workdir", ".")
        if workdir.startswith("/") or ".." in workdir.split("/"):
            raise ValueError(f"NAVIGATOR_REPOS_FILE workdir must stay inside the repo: {workdir!r}")
        cmds = entry.get("allowed_commands")
        repos.append(RepoConfig(path=entry["path"], workdir=workdir,
                                squash=bool(entry.get("squash", False)),
                                brief=entry.get("brief"),
                                context_files=entry.get("context_files"),
                                min_plan_interval_hours=entry.get("min_plan_interval_hours"),
                                max_proposals_per_tick=entry.get("max_proposals_per_tick"),
                                allowed_commands=_check_allowlist("NAVIGATOR_REPOS_FILE", cmds)
                                if cmds is not None else None))
    return repos

def _allowlist_env(name: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(DEFAULT_ALLOWLIST)
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    for e in entries:
        if any(ch in _ALLOWLIST_FORBIDDEN for ch in e):
            raise ValueError(f"{name} entry contains forbidden character: {e!r}")
    return entries
