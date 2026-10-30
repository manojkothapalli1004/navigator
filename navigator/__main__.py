import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog
from navigator.cli import COMMANDS, _safe, run_cli
from navigator.config import (ENV_ONLY_FIELDS, HOT_FIELDS, WIRING_FIELDS, RepoConfig, Settings,
                              effective_repos)
from navigator.runtime.fleet import Fleet
from navigator.contracts.enums import Role
from navigator.observability import decisions
from navigator.observability.notify import NotifyingACT, make_notifier
from navigator.observability.telegram_inbox import (TelegramError, apply_commands,
                                                    make_telegram_inbox)
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.clients.orc_client import FakeORCClient, HTTPORCClient
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
from navigator.runtime.executor import code_sha
from navigator.runtime.loop import Navigator
from navigator.runtime.outcome_store import OutcomeStore
from navigator.runtime.triage import Triage
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import CommandRunner
from navigator.safety.errors import QuotaExhaustedError

def _make_driver(settings: Settings):
    if settings.driver == "fake":
        return FakeSubagentDriver()
    if settings.driver == "claude_sdk":
        # from_settings wires per-role models, the model fallback chain
        # (NAVIGATOR_SDK_MODEL_FALLBACKS / _COOLDOWN_S) and one shared cooldown dict.
        # make_sdk_query_fn is passed from this module so tests can monkeypatch it.
        return ClaudeSDKDriver.from_settings(settings,
                                             budget_guard=BudgetGuard(settings.max_cost_usd),
                                             make_query_fn=make_sdk_query_fn)
    raise ValueError(f"unknown NAVIGATOR_DRIVER {settings.driver!r}")

def build_navigator(settings: Settings):
    repos = effective_repos(settings)
    if not repos:
        raise ValueError("NAVIGATOR_REPO must point at the target git repository")
    notifier = make_notifier()
    act = HTTPACTClient(settings.act_base_url) if settings.act_base_url else FakeACTClient()
    act = NotifyingACT(act, notifier)
    orc = HTTPORCClient(settings.orc_base_url) if settings.orc_base_url else FakeORCClient()
    driver = _make_driver(settings)
    navs = []
    for rc in repos:
        runner = CommandRunner(allowlist=rc.allowed_commands or settings.allowed_commands,
                               worktree_root=settings.worktree_root,
                               # capped at 300s (build 2): when asyncio.wait_for cancels the builder
                               # query, a run_tests to_thread worker keeps running and asyncio.run
                               # waits at most 300s for the default executor on Python 3.12; a longer
                               # runner timeout would let pytest outlive the query with os.environ
                               # still sealed. The verifier shares this runner, so its per-command
                               # bound is capped too (only bites when NAVIGATOR_COMMAND_TIMEOUT_S > 300). 2b: the clamp
                               # exists only for that worker-thread overrun, so it applies only while the
                               # tool is enabled; with it off the verifier keeps the configured bound
                               timeout_s=(min(settings.command_timeout_s, 300.0) if settings.builder_tests
                                          else settings.command_timeout_s))
        nav = Navigator(act=act, orc=orc, driver=driver, runner=runner, settings=settings,
                        repo=rc.path, lock_path=settings.lock_path, workdir=rc.workdir,
                        squash=rc.squash, brief=rc.brief, context_files=rc.context_files,
                        min_plan_interval_hours=rc.min_plan_interval_hours,
                        max_proposals_per_tick=rc.max_proposals_per_tick)
        nav.notifier = notifier
        navs.append(nav)
    if len(navs) == 1:
        target = navs[0]
    else:
        target = Fleet(navs)
        target.notifier = notifier
    # opt-in (NAVIGATOR_TELEGRAM_COMMANDS=1): operator merge decisions from Telegram
    target.telegram_inbox = make_telegram_inbox()
    target.telegram_act = act
    # one triage pass across every repo's proposals: the operator sees a top 3
    target.triage = Triage(act, query_fn=_triage_query_fn(driver), notifier=notifier,
                           path=os.getenv("NAVIGATOR_TRIAGE_PATH")
                           or str(Path(settings.outcome_path).with_name("triage.json")),
                           outcomes=lambda: OutcomeStore(settings.outcome_path).latest(10))
    return target

def _triage_query_fn(driver):
    """The planner's model ranks proposals; the fake driver has none (fallback order)."""
    if not isinstance(driver, ClaudeSDKDriver):
        return None
    return driver.role_query_fns.get(Role.planner, driver.query_fn)

def _head_sha(repo: str) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

def _telegram_error_kind(e: Exception) -> str:
    """What the operator is told. A TelegramError's message is fixed text; any other
    exception is named only, since its text could carry the token-bearing URL."""
    if isinstance(e, TelegramError):
        return f"{type(e).__name__}: {e}"
    return type(e).__name__


def _telegram_step(nav, summary: dict) -> None:
    """Poll the operator's Telegram chat once and record merge decisions in ACT.
    A failure here must never break the cycle: it goes into the summary, and the
    operator is told ONCE per distinct error kind (e.g. a second getUpdates consumer
    answered with 409 every cycle), not every cycle. A successful poll re-arms the
    notice."""
    inbox = getattr(nav, "telegram_inbox", None)
    if inbox is None:
        return
    try:
        commands = inbox.poll_once()
        act = getattr(nav, "telegram_act", None) or nav.act
        results = apply_commands(act, commands, nav.notifier)
        summary["telegram_commands"] = len(commands)
        summary["telegram_applied"] = sum(1 for r in results if r.get("applied"))
        nav.telegram_last_error = None
    except Exception as e:
        summary["telegram_error"] = f"{type(e).__name__}: {e}"[:300]
        kind = _telegram_error_kind(e)
        if getattr(nav, "telegram_last_error", None) == kind:
            return
        nav.telegram_last_error = kind
        try:
            nav.notifier.send(f"Navigator: Telegram commands not polled - {kind} "
                              f"(no further notice until this changes)")
        except Exception:
            pass

def _triage_step(nav, summary: dict) -> None:
    """Retire repeats and rank before planning, so freed backlog room is used this cycle.
    A triage failure never breaks the cycle."""
    triage = getattr(nav, "triage", None)
    if triage is None:
        return
    try:
        summary.update(triage.run())
    except Exception as e:
        summary["triage_error"] = f"{type(e).__name__}: {e}"[:300]

def _decisions_step(nav, summary: dict) -> None:
    """The decisions inbox tick (observability/decisions.py): the 4h nudge and the 8am
    digest for everything waiting on the operator. Never breaks the cycle: an exception
    goes into the summary as its class name ONLY (a notifier's error text can carry the
    token-bearing URL); a tick that could not write its state file is reported too."""
    try:
        act = getattr(nav, "telegram_act", None) or nav.act
        result = decisions.decisions_tick(act, nav.notifier)
        summary["decisions"] = result
        if isinstance(result, dict) and result.get("state_write_failed"):
            summary["decisions_error"] = "state_write_failed"
    except Exception as e:
        summary["decisions_error"] = type(e).__name__

def _cycle(nav: Navigator, settings: Settings) -> dict:
    summary: dict = {"mode": settings.mode, "driver": settings.driver}
    _triage_step(nav, summary)
    # planning failures (LLM limits, timeouts) must not block approved work or merges
    try:
        summary["proposals_created"] = len(nav.periodic_tick())
    except QuotaExhaustedError as e:
        # N16: class name + bounded reset time only; the SDK text may carry a login URL
        summary["proposals_created"] = 0
        summary["planner_error"] = f"{type(e).__name__}: resets_at={e.resets_at}"
    except Exception as e:
        summary["proposals_created"] = 0
        summary["planner_error"] = f"{type(e).__name__}: {e}"[:300]
    summary["runs_executed"] = len(nav.approval_poll())
    # N16: a build deferred on an account-wide limit is reported, not counted as a failure
    deferral = getattr(nav, "quota_deferral", None)
    if deferral:
        summary["quota_exhausted"] = dict(deferral)
    paused = getattr(nav, "quota_paused_until", None)
    if paused:
        summary["quota_paused_until"] = paused.isoformat(timespec="seconds")
    _telegram_step(nav, summary)
    _decisions_step(nav, summary)
    summary["runs_finalized"] = len(nav.merge_poll())
    summary["snapshot_chars"] = nav.last_snapshot_chars
    return summary

def _reset_budget(nav: Navigator) -> None:
    # The budget cap is per-cycle, not process-lifetime; a long-running daemon
    # must zero accumulated spend each tick or it locks out after a few cycles.
    guard = getattr(nav.driver, "budget_guard", None)
    if guard is not None:
        guard.reset()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ACT restarts take ~1 min to accept connections; Navigator racing it at boot
# crash-looped 4x on 2026-07-17. Wait bounded, then proceed \u2014 the cycle loop's
# own error handling covers a dependency that stays down.
_RECONCILE_ATTEMPTS = 18
_RECONCILE_DELAY_S = 10.0

def _reconcile_with_readiness(nav: Navigator) -> None:
    for attempt in range(1, _RECONCILE_ATTEMPTS + 1):
        try:
            nav.reconcile_on_startup()
            return
        except (httpx.ConnectError, ConnectionError) as e:
            print(json.dumps({"ts": _now_iso(), "startup": "waiting_for_dependencies",
                              "attempt": attempt, "error": f"{type(e).__name__}: {e}"[:200]}),
                  flush=True)
            if attempt == _RECONCILE_ATTEMPTS:
                return
            time.sleep(_RECONCILE_DELAY_S)

# ---- U2a: settings hot reload from the ACT document navigator/settings --------------------
# The daemon starts on Settings.from_env() (`base`). At the top of each cycle it reads the
# document and overlays its validated hot fields on `base` (document > env > default; the
# env-only safety fields are refused, see config.HOT_FIELDS / ENV_ONLY_FIELDS). Nothing in
# this path may fail or stop a cycle: an ACT error keeps the current settings. With no
# document the daemon behaves byte-identically to before (no settings_* key in a cycle line).
_SETTINGS_DOC = "navigator/settings"
_STATUS_DOC = "navigator/status"
_STATUS_ACTOR = "navigator-daemon"


def _doc_act(nav):
    """The ACT client used for the settings/status documents, or None when the navigator has
    none with a get_doc (the bare FakeNav doubles in tests): then nothing is reloaded."""
    act = getattr(nav, "telegram_act", None) or getattr(nav, "act", None)
    if act is not None and callable(getattr(act, "get_doc", None)):
        return act
    return None


def _members(target) -> list:
    """The single-repo navigators behind `target`: a Fleet's members, else target itself."""
    navigators = getattr(target, "navigators", None)
    return list(navigators) if navigators else [target]


def _reload_settings(act, base: Settings, applied: tuple[int, dict] | None):
    """Read navigator/settings. Returns (new_settings, applied, rejected, error) where
    `applied` is the (version, body) pair the caller should keep:
    - no document: (None, None, [], None); a caller holding an applied document returns to base
    - unchanged (same version AND body as `applied`): (None, applied, [], None)
    - new document: (base.overlay(body)[0], (version, body), rejected, None)
    - any exception: (None, applied, [], "<Class>: <msg>") meaning keep the current settings.
    A deleted-and-recreated document at the same version but a different body counts as new."""
    try:
        doc = act.get_doc(_SETTINGS_DOC)
        if doc is None:
            return None, None, [], None
        body, version = doc
        if applied is not None and version == applied[0] and body == applied[1]:
            return None, applied, [], None
        new, rejected = base.overlay(body)
        return new, (version, body), rejected, None
    except Exception as e:
        return None, applied, [], f"{type(e).__name__}: {e}"[:200]


def _apply_settings(target, new: Settings, old: Settings) -> tuple[Settings, str | None]:
    """Put `new` into effect on every member of `target`. Returns (the settings ACTUALLY
    applied, error or None). A change to a WIRING_FIELDS value rebuilds the ClaudeSDKDriver
    with the SAME budget guard and cooldown dict; if that rebuild fails for ANY reason the old
    driver stays and only the non-wiring fields apply. A FakeSubagentDriver is never rebuilt."""
    applied = new
    error = None
    driver = getattr(target, "driver", None)
    if (isinstance(driver, ClaudeSDKDriver)
            and any(getattr(new, f) != getattr(old, f) for f in WIRING_FIELDS)):
        try:
            rebuilt = ClaudeSDKDriver.from_settings(new, budget_guard=driver.budget_guard,
                                                    make_query_fn=make_sdk_query_fn,
                                                    cooldowns=getattr(driver, "cooldowns", None))
        except Exception as e:
            applied = replace(new, **{f: getattr(old, f) for f in WIRING_FIELDS})
            error = f"{type(e).__name__}: {e}"[:300]
        else:
            for nav in _members(target):
                nav.driver = rebuilt
            triage = getattr(target, "triage", None)
            if triage is not None:
                triage.query_fn = _triage_query_fn(rebuilt)
    for nav in _members(target):
        nav.settings = applied
        runner = getattr(nav, "runner", None)
        if runner is not None:
            # the same clamp build_navigator applies (see the comment there)
            runner.timeout_s = (min(applied.command_timeout_s, 300.0) if applied.builder_tests
                                else applied.command_timeout_s)
    return applied, error


def _env_only_value(settings: Settings, name: str) -> str:
    """Redacted printable form of an env-only field for the status document."""
    v = getattr(settings, name)
    if name == "repos":
        text = json.dumps([r.path for r in v])
    elif isinstance(v, list):
        text = json.dumps(v)
    else:
        text = str(v)
    return _safe(text)


def _write_status(act, target, settings: Settings, version, cycle: int, summary: dict, *,
                  error: str | None = None, rejected: list[str] | None = None) -> str | None:
    """Write navigator/status for the UI after a cycle: the effective hot fields, the redacted
    env-only fields and the last settings error / rejected keys (they persist here until a
    later reload clears them). Never raises: returns None, or the failing exception's class
    name (its text could carry a URL)."""
    try:
        body = {"ts": _now_iso(), "cycle": cycle, "code_sha": code_sha(),
                "settings_version": version, "members": len(_members(target)),
                "summary": summary,
                "effective": {f: getattr(settings, f) for f in sorted(HOT_FIELDS)},
                "env_only": {f: _env_only_value(settings, f) for f in sorted(ENV_ONLY_FIELDS)},
                "settings_error": error,
                "settings_rejected": list(rejected or [])}
        act.put_doc(_STATUS_DOC, body, actor=_STATUS_ACTOR)
    except Exception as e:
        return type(e).__name__
    return None


_FAILURE_WARN_THRESHOLD = 4

def run_daemon(nav: Navigator, settings: Settings, *, max_cycles: int | None = None) -> None:
    # which navigator code is running: lets outcomes/cycles be tied to a commit
    print(json.dumps({"ts": _now_iso(), "startup": "navigator", "code_sha": code_sha()}), flush=True)
    _reconcile_with_readiness(nav)
    # U2a settings hot reload: `base` is the environment, `current` what this cycle runs with
    # (document > env > default), `applied` the (version, body) of the document in force
    base = settings
    current = settings
    applied: tuple[int, dict] | None = None
    last_error: str | None = None   # shown in navigator/status until a later reload clears it
    last_rejected: list[str] = []
    cycle = 0
    consecutive_failures = 0
    while True:
        cycle += 1
        act = _doc_act(nav)
        settings_error: str | None = None
        rejected: list[str] = []
        if act is not None:
            try:
                new, applied_next, rejected, settings_error = _reload_settings(act, base, applied)
                if (new is None and applied_next is None and applied is not None
                        and settings_error is None):
                    new = base  # the document is gone: back to the environment settings
                if new is not None:
                    if new != current:
                        current, apply_error = _apply_settings(nav, new, current)
                        settings_error = settings_error or apply_error
                    applied = applied_next
                    last_rejected = list(rejected)
                    last_error = settings_error
                elif settings_error is not None:
                    last_error = settings_error
            except Exception as e:  # nothing in reload/apply may kill the daemon or fail a cycle
                settings_error = f"{type(e).__name__}: {e}"[:300]
                last_error = settings_error
        _reset_budget(nav)
        try:
            summary = _cycle(nav, current)
        except Exception as e:  # a dependency blip must not kill the daemon; retry next tick
            summary = {"error": f"{type(e).__name__}: {e}"[:300]}
        # settings_* keys only when they carry information: no document = byte-identical lines
        if applied is not None:
            summary["settings_version"] = applied[0]
        if rejected:
            summary["settings_rejected"] = list(rejected)
        if settings_error is not None:
            summary["settings_error"] = settings_error
        failure = summary.get("error") or summary.get("planner_error")
        if not failure:
            if consecutive_failures >= _FAILURE_WARN_THRESHOLD:
                nav.notifier.send(f"Navigator recovered after {consecutive_failures} failed cycles")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            summary.setdefault("error", failure)
            if consecutive_failures >= _FAILURE_WARN_THRESHOLD:
                summary["consecutive_failures"] = consecutive_failures
            if consecutive_failures == _FAILURE_WARN_THRESHOLD:
                nav.notifier.send(f"Navigator cycles failing: {consecutive_failures} in a row. "
                                  f"Latest: {summary['error']}")
        if act is not None:
            write_error = _write_status(act, nav, current, applied[0] if applied else None,
                                        cycle, summary, error=last_error, rejected=last_rejected)
            if write_error is not None:
                summary["status_write_error"] = write_error
        print(json.dumps({"ts": _now_iso(), "cycle": cycle, **summary}), flush=True)
        if max_cycles is not None and cycle >= max_cycles:
            return
        time.sleep(current.tick_interval_s)

def _configure_logging() -> None:
    # stdout carries one JSON summary per cycle; human logs must not interleave
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "telegram-reader":
        # N11: the always-on Telegram reader (long poll); imported lazily so the daemon
        # and the CLI do not pay for it
        from navigator.observability.telegram_reader import main_reader
        return main_reader()
    if argv and argv[0] in COMMANDS:
        url = os.getenv("NAVIGATOR_ACT_BASE_URL") or "http://127.0.0.1:8090"
        return run_cli(argv, act=HTTPACTClient(url), out=sys.stdout)
    parser = argparse.ArgumentParser(prog="navigator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="run a single perceive/plan/poll cycle")
    group.add_argument("--max-cycles", type=int, default=None,
                       help="daemon mode: stop after N cycles (default: run forever)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    nav = build_navigator(settings)
    if args.once:
        nav.reconcile_on_startup()
        print(json.dumps(_cycle(nav, settings)))
    else:
        run_daemon(nav, settings, max_cycles=args.max_cycles)
    return 0

if __name__ == "__main__":
    sys.exit(main())
