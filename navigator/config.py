import dataclasses
import json
import math
import os
import zoneinfo
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
    tier_c_paths: list[str] | None = None   # extra merge-tier C patterns (merge_tiers.classify)
    tier_floor: str = "A"                    # lowest merge tier for this repo: "A" | "B" | "C"


@dataclass
class Settings:
    driver: str = "fake"
    mode: str = "dry_run"
    # N8: apply operator merge decisions while mode == "dry_run" (builds stay off)
    merge_in_dry_run: bool = False
    # N12d: after a merge, rebase the repo's other runs parked at the merge gate onto the
    # new HEAD and re-verify them with their recorded commands (no model call)
    rebase_stale_runs: bool = True
    # operator decision 2026-10-28 ('ask me less'): operator-origin proposals start approved;
    # periodic ones still need approve
    operator_preapprove: bool = False
    act_base_url: str | None = None
    orc_base_url: str | None = None
    worktree_root: str = "/tmp/navigator-worktrees"
    tick_interval_s: int = 900
    allowed_commands: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWLIST))
    max_tool_failures: int = 5
    max_identical_failures: int = 3
    max_run_duration_s: int = 1800
    # extra builder attempts in the same run (same worktree) after a repairable verify
    # failure (tests, skip gate, missing tests); 0 = off
    builder_repair_turns: int = 1
    max_cost_usd: float = 2.0
    max_proposals_per_tick: int = 3
    max_open_proposals: int = 10
    # planner throttle (loop._planner_throttled): below this merge rate over the last window of this repo's periodic
    # outcomes the planner only probes once per throttle_hours; 0.0 = off
    planner_min_merge_rate: float = 0.0
    planner_merge_window: int = 20
    planner_throttle_hours: float = 24.0
    memory_chars_per_section: int = 3000
    proposal_ttl_days: int = 7
    command_timeout_s: float = 60.0
    max_failed_tests: int = 50
    # merge tiers (merge_tiers.py): A auto-merges, B merges on 'merge all', C always needs its own word
    merge_tier_a_auto: bool = False
    merge_tier_b_batch: bool = False
    repo: str | None = None
    repos: list[RepoConfig] = field(default_factory=list)
    lock_path: str = "/tmp/navigator.lock"
    outcome_path: str = "/tmp/navigator-outcomes.jsonl"
    # quota pause file shared by the daemon and once.sh: JSON {"until": ISO}; while
    # `until` is in the future no planner call and no build starts. None = off
    quota_pause_path: str | None = None
    sdk_model: str | None = None
    sdk_role_models: dict[str, str] = field(default_factory=dict)
    sdk_timeout_s: float = 300.0
    # ordered model fallback chain; empty = fallback disabled
    sdk_model_fallbacks: list[str] = field(default_factory=lambda: list(DEFAULT_SDK_MODEL_FALLBACKS))
    sdk_model_cooldown_s: int = DEFAULT_SDK_MODEL_COOLDOWN_S
    # builder read-only tools (operator decision 2026-10-28): when on, the BUILDER role's SDK
    # session gets Read/Grep/Glob with cwd = its run worktree; writes still arrive as JSON and
    # pass every gate; planner and verifier stay tool-less. Off = byte-identical to before.
    builder_tools: bool = False
    sdk_builder_max_turns: int = 30
    sdk_builder_timeout_s: float = 900.0
    # builder test runner (build 2): when on, the tooled builder may run the repo's
    # allowlisted pytest entry inside its worktree at most builder_test_runs times per build (first attempt +
    # repair turns share one budget); requires builder_tools
    builder_tests: bool = False
    builder_test_runs: int = 5
    # U2a settings hot reload: decisions-inbox knobs settable from the UI document only (not
    # read from the environment). None = observability/decisions.py keeps reading its own env
    # variables; a later build makes decisions.py consult these.
    digest_hour: int | None = None
    decisions_nudges: bool | None = None
    decisions_tz: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            driver=os.getenv("NAVIGATOR_DRIVER", "fake"),
            mode=os.getenv("NAVIGATOR_MODE", "dry_run"),
            merge_in_dry_run=_bool_env("NAVIGATOR_MERGE_IN_DRY_RUN", "0"),
            rebase_stale_runs=_bool_env("NAVIGATOR_REBASE_STALE_RUNS", "1"),
            operator_preapprove=_bool_env("NAVIGATOR_OPERATOR_PREAPPROVE", "0"),
            act_base_url=os.getenv("NAVIGATOR_ACT_BASE_URL"),
            orc_base_url=os.getenv("NAVIGATOR_ORC_BASE_URL"),
            worktree_root=os.getenv("NAVIGATOR_WORKTREE_ROOT", "/tmp/navigator-worktrees"),
            tick_interval_s=_int_env("NAVIGATOR_TICK_INTERVAL_S", "900"),
            allowed_commands=_allowlist_env("NAVIGATOR_ALLOWED_COMMANDS"),
            max_tool_failures=_positive_int_env("NAVIGATOR_MAX_TOOL_FAILURES", "5"),
            max_identical_failures=_int_env("NAVIGATOR_MAX_IDENTICAL_FAILURES", "3"),
            max_run_duration_s=_int_env("NAVIGATOR_MAX_RUN_DURATION_S", "1800"),
            builder_repair_turns=_non_negative_int_env("NAVIGATOR_BUILDER_REPAIR_TURNS", "1"),
            max_cost_usd=_positive_float_env("NAVIGATOR_MAX_COST_USD", "2.0"),
            max_proposals_per_tick=_int_env("NAVIGATOR_MAX_PROPOSALS_PER_TICK", "3"),
            max_open_proposals=_positive_int_env("NAVIGATOR_MAX_OPEN_PROPOSALS", "10"),
            planner_min_merge_rate=_ratio_env("NAVIGATOR_PLANNER_MIN_MERGE_RATE", "0"),
            planner_merge_window=_positive_int_env("NAVIGATOR_PLANNER_MERGE_WINDOW", "20"),
            planner_throttle_hours=_positive_float_env("NAVIGATOR_PLANNER_THROTTLE_HOURS", "24"),
            memory_chars_per_section=_positive_int_env("NAVIGATOR_MEMORY_CHARS_PER_SECTION", "3000"),
            proposal_ttl_days=_positive_int_env("NAVIGATOR_PROPOSAL_TTL_DAYS", "7"),
            command_timeout_s=_positive_float_env("NAVIGATOR_COMMAND_TIMEOUT_S", "60"),
            max_failed_tests=_int_env("NAVIGATOR_MAX_FAILED_TESTS", "50"),
            merge_tier_a_auto=_bool_env("NAVIGATOR_MERGE_TIER_A_AUTO", "0"),
            merge_tier_b_batch=_bool_env("NAVIGATOR_MERGE_TIER_B_BATCH", "0"),
            repo=os.getenv("NAVIGATOR_REPO"),
            repos=_repos_env(),
            lock_path=os.getenv("NAVIGATOR_LOCK_PATH", "/tmp/navigator.lock"),
            outcome_path=os.getenv("NAVIGATOR_OUTCOME_PATH", "/tmp/navigator-outcomes.jsonl"),
            quota_pause_path=os.getenv("NAVIGATOR_QUOTA_PAUSE_PATH") or None,
            sdk_model=os.getenv("NAVIGATOR_SDK_MODEL"),
            sdk_role_models={role: os.environ[f"NAVIGATOR_SDK_MODEL_{role.upper()}"]
                             for role in ("planner", "builder", "verifier")
                             if os.getenv(f"NAVIGATOR_SDK_MODEL_{role.upper()}")},
            sdk_timeout_s=_positive_float_env("NAVIGATOR_SDK_TIMEOUT_S", "300.0"),
            sdk_model_fallbacks=_model_list_env("NAVIGATOR_SDK_MODEL_FALLBACKS",
                                                ",".join(DEFAULT_SDK_MODEL_FALLBACKS)),
            sdk_model_cooldown_s=_positive_int_env("NAVIGATOR_SDK_MODEL_COOLDOWN_S",
                                                   str(DEFAULT_SDK_MODEL_COOLDOWN_S)),
            builder_tools=_bool_env("NAVIGATOR_BUILDER_TOOLS", "0"),
            sdk_builder_max_turns=_positive_int_env("NAVIGATOR_SDK_BUILDER_MAX_TURNS", "30"),
            sdk_builder_timeout_s=_positive_float_env("NAVIGATOR_SDK_BUILDER_TIMEOUT_S", "900.0"),
            builder_tests=_bool_env("NAVIGATOR_BUILDER_TESTS", "0"),
            builder_test_runs=_positive_int_env("NAVIGATOR_BUILDER_TEST_RUNS", "5"),
        )

    def overlay(self, doc) -> tuple["Settings", list[str]]:
        """Settings hot reload (U2a): the ACT document navigator/settings over self.

        Returns (settings, rejected). Only HOT_FIELDS may be set, each validated with JSON
        types (a bool is never accepted where an int is expected); ENV_ONLY_FIELDS and
        unknown keys are refused. A refused key is reported as "<key>: <reason>" and skipped,
        the other keys still apply. Keys are processed in sorted order. A document that is
        not an object changes nothing.
        """
        if not isinstance(doc, dict):
            return self, ["<document>: not an object"]
        accepted: dict = {}
        rejected: list[str] = []
        for key in sorted(doc, key=str):
            value = doc[key]
            if key in ENV_ONLY_FIELDS:
                rejected.append(f"{key}: env-only")
                continue
            if key not in HOT_FIELDS:
                rejected.append(f"{key}: unknown")
                continue
            reason = _hot_value_error(key, value)
            if reason is not None:
                rejected.append(f"{key}: {reason}")
                continue
            accepted[key] = _hot_value(key, value)
        return dataclasses.replace(self, **accepted), rejected


# ---- U2a settings hot reload: which Settings fields the UI document may set -----------------
# HOT_FIELDS: settable from the ACT document navigator/settings (validated in Settings.overlay).
# WIRING_FIELDS: the hot fields a ClaudeSDKDriver is built from; a change rebuilds the driver.
# ENV_ONLY_FIELDS: safety/plumbing fields that only the environment may set, never the document.
# HOT_FIELDS | ENV_ONLY_FIELDS partitions the Settings fields (tests/test_config_overlay.py).
HOT_FIELDS = frozenset({
    "sdk_model", "sdk_role_models", "mode", "merge_in_dry_run", "operator_preapprove",
    "merge_tier_a_auto", "merge_tier_b_batch", "rebase_stale_runs", "builder_tools",
    "builder_tests", "builder_repair_turns", "builder_test_runs", "tick_interval_s",
    "max_open_proposals", "max_proposals_per_tick", "proposal_ttl_days", "planner_min_merge_rate",
    "planner_merge_window", "planner_throttle_hours", "sdk_builder_max_turns", "sdk_timeout_s",
    "sdk_builder_timeout_s", "digest_hour", "decisions_nudges", "decisions_tz",
})
WIRING_FIELDS = frozenset({
    "sdk_model", "sdk_role_models", "sdk_timeout_s", "builder_tools", "builder_tests",
    "builder_test_runs", "sdk_builder_max_turns", "sdk_builder_timeout_s",
})
ENV_ONLY_FIELDS = frozenset({
    "driver", "act_base_url", "orc_base_url", "worktree_root", "allowed_commands",
    "max_tool_failures", "max_identical_failures", "max_run_duration_s", "max_cost_usd",
    "memory_chars_per_section", "command_timeout_s", "max_failed_tests", "repo", "repos",
    "lock_path", "outcome_path", "quota_pause_path", "sdk_model_fallbacks", "sdk_model_cooldown_s",
})

_HOT_BOOL_FIELDS = frozenset({"merge_in_dry_run", "operator_preapprove", "merge_tier_a_auto",
                              "merge_tier_b_batch", "rebase_stale_runs", "builder_tools",
                              "builder_tests", "decisions_nudges"})
# int fields with their inclusive (lower, upper) bounds (rev1: every int hot field is bounded
# above too, so a document can never hand the daemon a value like time.sleep(10**10))
_HOT_INT_RANGE: dict[str, tuple[int, int]] = {
    "builder_test_runs": (1, 50), "max_open_proposals": (1, 100), "max_proposals_per_tick": (1, 20),
    "proposal_ttl_days": (1, 365), "planner_merge_window": (1, 1000), "sdk_builder_max_turns": (1, 200),
    "builder_repair_turns": (0, 5), "tick_interval_s": (60, 86400),
}
# positive float fields with their inclusive upper bound; a value must also be finite
# (json.loads accepts Infinity/NaN, which would disable a timeout or stop the planner)
_HOT_FLOAT_MAX: dict[str, int] = {"planner_throttle_hours": 720, "sdk_timeout_s": 7200,
                                  "sdk_builder_timeout_s": 7200}
_SDK_ROLES = ("planner", "builder", "verifier")


def _is_json_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_json_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _hot_value_error(key: str, v) -> str | None:
    """Short fixed reason a document value is refused for hot field `key`; None when valid."""
    if key in _HOT_BOOL_FIELDS:
        return None if isinstance(v, bool) else "must be a boolean"
    if key == "mode":
        return None if v in ("dry_run", "execute") else "must be dry_run or execute"
    if key == "sdk_model":
        return None if isinstance(v, str) and v else "must be a non-empty string"
    if key == "sdk_role_models":
        if not isinstance(v, dict):
            return "must be an object"
        if not set(v) <= set(_SDK_ROLES):
            return "keys must be planner, builder or verifier"
        if not all(isinstance(model, str) and model for model in v.values()):
            return "values must be non-empty strings"
        return None
    if key in _HOT_INT_RANGE:
        lo, hi = _HOT_INT_RANGE[key]
        return None if _is_json_int(v) and lo <= v <= hi else f"must be an integer {lo}..{hi}"
    if key == "digest_hour":
        return None if _is_json_int(v) and 0 <= v <= 23 else "must be an integer 0..23"
    if key in _HOT_FLOAT_MAX:
        hi = _HOT_FLOAT_MAX[key]
        reason = f"must be a number > 0 and <= {hi}"
        # order matters: type check, then finiteness on floats ONLY (math.isfinite on a huge
        # int such as 10**400 would raise OverflowError), then the range check (which also
        # refuses any int above the bound without converting it to float)
        if not _is_json_number(v):
            return reason
        if isinstance(v, float) and not math.isfinite(v):
            return reason
        return None if 0 < v <= hi else reason
    if key == "planner_min_merge_rate":
        reason = "must be a number between 0 and 1"
        if not _is_json_number(v):
            return reason
        if isinstance(v, float) and not math.isfinite(v):
            return reason
        return None if 0.0 <= v <= 1.0 else reason
    if key == "decisions_tz":
        if not isinstance(v, str) or not v:
            return "must be a time zone name"
        try:
            zoneinfo.ZoneInfo(v)
        except Exception:
            return "unknown time zone"
        return None
    return "unknown"


def _hot_value(key: str, v):
    """The value stored for a validated hot field (no aliasing of the document's objects)."""
    if key == "sdk_role_models":
        return dict(v)
    if key in _HOT_FLOAT_MAX or key == "planner_min_merge_rate":
        return float(v)
    return v


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


def _non_negative_int_env(name: str, default: str) -> int:
    v = _int_env(name, default)
    if v < 0:
        raise ValueError(f"{name} must be >= 0, got {v}")
    return v


def _positive_float_env(name: str, default: str) -> float:
    v = _float_env(name, default)
    if v <= 0:
        raise ValueError(f"{name} must be > 0, got {v}")
    return v


def _ratio_env(name: str, default: str) -> float:
    v = _float_env(name, default)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {v}")
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
        tier_c_paths = entry.get("tier_c_paths")
        if tier_c_paths is not None and (not isinstance(tier_c_paths, list)
                                         or not all(isinstance(p, str) for p in tier_c_paths)):
            raise ValueError("NAVIGATOR_REPOS_FILE tier_c_paths must be a list of strings")
        tier_floor = entry.get("tier_floor", "A")
        if tier_floor not in ("A", "B", "C"):
            raise ValueError(f"NAVIGATOR_REPOS_FILE tier_floor must be A, B or C, got {tier_floor!r}")
        repos.append(RepoConfig(path=entry["path"], workdir=workdir,
                                squash=bool(entry.get("squash", False)),
                                brief=entry.get("brief"),
                                context_files=entry.get("context_files"),
                                min_plan_interval_hours=entry.get("min_plan_interval_hours"),
                                max_proposals_per_tick=entry.get("max_proposals_per_tick"),
                                tier_c_paths=tier_c_paths,
                                tier_floor=tier_floor,
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
