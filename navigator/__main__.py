import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import structlog
from navigator.cli import COMMANDS, run_cli
from navigator.config import RepoConfig, Settings, effective_repos
from navigator.runtime.fleet import Fleet
from navigator.contracts.enums import Role
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
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import CommandRunner

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
                               timeout_s=settings.command_timeout_s)
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
    return target

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

def _cycle(nav: Navigator, settings: Settings) -> dict:
    summary: dict = {"mode": settings.mode, "driver": settings.driver}
    # planning failures (LLM limits, timeouts) must not block approved work or merges
    try:
        summary["proposals_created"] = len(nav.periodic_tick())
    except Exception as e:
        summary["proposals_created"] = 0
        summary["planner_error"] = f"{type(e).__name__}: {e}"[:300]
    summary["runs_executed"] = len(nav.approval_poll())
    _telegram_step(nav, summary)
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

_FAILURE_WARN_THRESHOLD = 4

def run_daemon(nav: Navigator, settings: Settings, *, max_cycles: int | None = None) -> None:
    # which navigator code is running: lets outcomes/cycles be tied to a commit
    print(json.dumps({"ts": _now_iso(), "startup": "navigator", "code_sha": code_sha()}), flush=True)
    _reconcile_with_readiness(nav)
    cycle = 0
    consecutive_failures = 0
    while True:
        cycle += 1
        _reset_budget(nav)
        try:
            summary = _cycle(nav, settings)
        except Exception as e:  # a dependency blip must not kill the daemon; retry next tick
            summary = {"error": f"{type(e).__name__}: {e}"[:300]}
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
        print(json.dumps({"ts": _now_iso(), "cycle": cycle, **summary}), flush=True)
        if max_cycles is not None and cycle >= max_cycles:
            return
        time.sleep(settings.tick_interval_s)

def _configure_logging() -> None:
    # stdout carries one JSON summary per cycle; human logs must not interleave
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    argv = sys.argv[1:] if argv is None else argv
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
