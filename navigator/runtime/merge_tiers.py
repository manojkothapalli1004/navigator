"""Merge tiers (operator decision 2026-10-28, 'ask me less').

A pure classifier: A = test files and data fixtures inside a tests directory, or documents/images
(whitelisted suffixes) only, C = money/auth/secret/migration/dependency paths,
Navigator's own safety code (only when the repo is named navigator) or the trader repo,
else B. Nothing here merges anything; the executor records the tier on every
run.awaiting_review event and later builds act on it. classify() never calls git.
"""
import fnmatch
import subprocess
from pathlib import Path

TIERS = ("A", "B", "C")

# C patterns are matched with fnmatch.fnmatchcase against the posix relpath AND every path component
# (_match_components); A patterns against the relpath and its basename only (_match); '*' crosses '/'
# 3a2: money, auth and key/cert/database paths fail closed to C (cold audit of 3a). Deliberately NOT 'pay*'
# (payload.py) and NOT '*order*' (recorder.py, border.css); '*perm*' accepts the odd false positive.
MONEY_C = ("*billing*", "*payment*", "*payout*", "*refund*", "pay.py", "order*", "*wallet*", "*ledger*",
           "*invoice*", "*stripe*", "*paypal*", "*checkout*")
AUTH_C = ("oauth*", "session*", "*perm*")
KEY_C = ("claude.md", "agents.md", ".claude/*", "gemini.md", "claude.local.md", ".claude", "*.crt", "*.cer", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*", "id_ecdsa*",
         "*api_key*", "*api-key*", "*apikey*", "*private*key*", "*.db", "*.sqlite*")
UNIVERSAL_C = ("*.env", "*.env.*", "*.pem", "*.key", "*secret*", "*credential*", "*token*", "*password*",
               "*migrations/*", "*alembic/*", "*.sql", "pyproject.toml", "uv.lock", "requirements*.txt",
               "auth*", "*security*", "deps.py", "*routes/*", "models.py", "db.py", "config.py", "settings.py",
               ".github/*", "deploy/*", "*.plist*", "*.sh", "legal.py", "constraints*.txt", "runtime.txt",
               ".envrc", "*.pth") + MONEY_C + AUTH_C + KEY_C
NAVIGATOR_C = ("navigator/safety/*", "navigator/runtime/phases.py", "navigator/runtime/executor.py",
               "navigator/runtime/loop.py", "navigator/runtime/merge_tiers.py", "navigator/runtime/worktree.py",
               "navigator/runtime/checkpoint.py", "navigator/runtime/drivers/*", "navigator/observability/telegram_*",
               "navigator/observability/actions.py", "navigator/cli.py", "navigator/config.py", "navigator/__main__.py",
               "navigator/runtime/fleet.py", "navigator/clients/act_client.py", "navigator/runtime/lock.py",
               "navigator/contracts/*")
TEST_A = ("tests/*", "*/tests/*")  # 3a2: directory only; a test_*.py outside a tests dir is code (B)
CONFTEST = "conftest.py"  # 3a2: runs code at import under the verifier: never A
DOC_A = ("*.md", "*.rst", "docs/*", "*.txt")
ALWAYS_C_REPOS = ("trader",)
# 3a3: tier A is a whitelist; anything else that used to be A is B
TEST_NAMES_A = ("test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx",
                "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx")
FIXTURE_SUFFIXES_A = (".json", ".csv", ".txt", ".md", ".yaml", ".yml", ".xml", ".snap", ".golden",
                      ".png", ".jpg", ".jpeg", ".gif", ".svg")
DOC_SUFFIXES_A = (".md", ".rst", ".txt", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def _normalize(path: str) -> str:
    posix = Path(path).as_posix()
    if posix.startswith("./"):
        posix = posix[2:]
    # NOT str.lstrip("./"): that strips CHARACTERS, turning ".env" into "env" and ".github/x" into "github/x"
    posix = posix.lstrip("/")
    # docs/SECRETS.md, API_TOKEN.txt must still hit *secret* / *token*; all patterns are lowercase
    return posix.lower()


def _match(path: str, patterns) -> bool:
    posix = _normalize(path)
    name = Path(posix).name
    return any(fnmatch.fnmatchcase(posix, pat) or fnmatch.fnmatchcase(name, pat) for pat in patterns)


def _match_components(path: str, patterns) -> bool:
    """Like _match, but every path component counts too, so "auth*" hits app/auth/jwt.py and app/authz/roles.py.
    C-only: letting DOC_A/TEST_A see components would make notes.md/build.go a doc (fail-open to A)."""
    posix = _normalize(path)
    parts = posix.split("/")
    return any(fnmatch.fnmatchcase(posix, pat) or any(fnmatch.fnmatchcase(part, pat) for part in parts)
               for pat in patterns)


def _is_c(path: str, extra) -> bool:
    return _match_components(path, UNIVERSAL_C) or _match_components(path, extra)


def _is_a(path: str) -> bool:
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if name == CONFTEST:
        return False
    # 3a3: inside a tests dir only test files and data fixtures (not pytest.ini, __init__.py, plugins)
    if _match(path, TEST_A) and (any(fnmatch.fnmatchcase(name, pat) for pat in TEST_NAMES_A)
                                 or suffix in FIXTURE_SUFFIXES_A):
        return True
    # docs: only document/image suffixes; requirements*.txt is a dependency file, not a doc
    return (_match(path, DOC_A) and suffix in DOC_SUFFIXES_A
            and not fnmatch.fnmatchcase(name, "requirements*.txt"))


def classify(changed, *, repo_name: str, tier_c_paths=(), tier_floor: str = "A") -> str:
    """A: every path is a test or doc file; C: trader repo, floor C, an empty/None change list, or any path
    matching a C pattern (universal, navigator's own safety modules when repo_name == "navigator", or
    tier_c_paths); else B. tier_floor raises the result (A < B < C)."""
    if tier_floor not in TIERS:
        raise ValueError(f"tier_floor must be one of {TIERS}, got {tier_floor!r}")
    if repo_name in ALWAYS_C_REPOS or tier_floor == "C" or not changed:
        return "C"
    if any(p != p.strip() for p in changed):
        return "C"  # a name with leading/trailing whitespace is nobody's honest change: fail closed (3a2)
    # paths are lowercased in _match, so per-repo patterns must be too (3a2)
    extra = tuple(p.lower() for p in (tier_c_paths or ())) + (NAVIGATOR_C if repo_name == "navigator" else ())
    if any(_is_c(p, extra) for p in changed):
        return "C"
    tier = "A" if all(_is_a(p) for p in changed) else "B"
    return tier if TIERS.index(tier) >= TIERS.index(tier_floor) else tier_floor


def repo_config_for(settings, repo_path: str):
    """The RepoConfig of effective_repos(settings) whose resolved path equals the resolved repo_path, else None."""
    from navigator.config import effective_repos  # inside the body: keeps the module import-light
    target = Path(repo_path).resolve()
    for cfg in effective_repos(settings):
        if cfg.path and Path(cfg.path).resolve() == target:
            return cfg
    return None


def classify_for_repo(changed, repo_path: str, cfg) -> str:
    return classify(changed, repo_name=Path(repo_path).name,
                    tier_c_paths=(cfg.tier_c_paths if cfg else None) or (),
                    tier_floor=cfg.tier_floor if cfg else "A")


def changed_files_for_run(repo: str, run) -> list | None:
    """`git diff --no-renames --name-only -z <run.base_commit_sha>..<run.verified_sha>` in repo
    (-z: NUL-separated and never quoted; core.quotePath=false still printed a name holding '"' or '\\'
    C-quoted, so no pattern could match it; --no-renames so a file moved out of a C path into tests/ or
    docs/ still lists its old path; the run's own commits; _rebase_one moves base_commit_sha, so this is
    right after a sibling merge). None when verified_sha is missing or git fails (a TimeoutExpired or
    OSError is also None, and so is a name that is not valid UTF-8: UnicodeDecodeError fails closed)."""
    base = getattr(run, "base_commit_sha", None)
    verified = getattr(run, "verified_sha", None)
    if not verified or not base:
        return None
    try:
        proc = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", f"{base}..{verified}"],
                              cwd=repo, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None
    if proc.returncode != 0:
        return None
    return [n for n in proc.stdout.split("\x00") if n]


def tier_for_run(repo: str, run, cfg) -> str | None:
    changed = changed_files_for_run(repo, run)
    return None if changed is None else classify_for_repo(changed, repo, cfg)


def batch_enabled(act=None) -> bool:
    """Whether 'merge all' is on for the Telegram-handling process (it has no Settings).
    U2b: with an ACT client that has get_doc, the settings document navigator/settings
    wins when its merge_tier_b_batch is a bool. A missing document, a missing or non-bool
    key, or ANY exception while reading falls back to NAVIGATOR_MERGE_TIER_B_BATCH exactly
    as before (a malformed value is off). Never raises."""
    if act is not None and callable(getattr(act, "get_doc", None)):
        try:
            doc = act.get_doc("navigator/settings")
            if doc is not None:
                value = doc[0].get("merge_tier_b_batch")
                if isinstance(value, bool):
                    return value
        except Exception:
            pass
    from navigator.config import _bool_env
    try:
        return _bool_env("NAVIGATOR_MERGE_TIER_B_BATCH", "0")
    except ValueError:
        return False
