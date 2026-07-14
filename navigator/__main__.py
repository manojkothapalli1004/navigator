import argparse
import json
import subprocess
import sys
import time
from navigator.config import Settings
from navigator.clients.act_client import FakeACTClient, HTTPACTClient
from navigator.clients.orc_client import FakeORCClient, HTTPORCClient
from navigator.runtime.drivers.claude_sdk_driver import ClaudeSDKDriver
from navigator.runtime.drivers.fake_driver import FakeSubagentDriver
from navigator.runtime.drivers.sdk_query import make_sdk_query_fn
from navigator.runtime.loop import Navigator
from navigator.safety.budget_guard import BudgetGuard
from navigator.safety.command_runner import CommandRunner

def _make_driver(settings: Settings):
    if settings.driver == "fake":
        return FakeSubagentDriver()
    if settings.driver == "claude_sdk":
        return ClaudeSDKDriver(query_fn=make_sdk_query_fn(model=settings.sdk_model),
                               budget_guard=BudgetGuard(settings.max_cost_usd))
    raise ValueError(f"unknown NAVIGATOR_DRIVER {settings.driver!r}")

def build_navigator(settings: Settings) -> Navigator:
    if not settings.repo:
        raise ValueError("NAVIGATOR_REPO must point at the target git repository")
    act = HTTPACTClient(settings.act_base_url) if settings.act_base_url else FakeACTClient()
    orc = HTTPORCClient(settings.orc_base_url) if settings.orc_base_url else FakeORCClient()
    runner = CommandRunner(allowlist=settings.allowed_commands, worktree_root=settings.worktree_root)
    return Navigator(act=act, orc=orc, driver=_make_driver(settings), runner=runner,
                     settings=settings, repo=settings.repo, lock_path=settings.lock_path)

def _head_sha(repo: str) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

def _cycle(nav: Navigator, settings: Settings) -> dict:
    created = nav.periodic_tick(base_commit_sha=_head_sha(settings.repo))
    runs = nav.approval_poll()
    return {"mode": settings.mode, "driver": settings.driver,
            "proposals_created": len(created), "runs_executed": len(runs)}

def _reset_budget(nav: Navigator) -> None:
    # The budget cap is per-cycle, not process-lifetime; a long-running daemon
    # must zero accumulated spend each tick or it locks out after a few cycles.
    guard = getattr(nav.driver, "budget_guard", None)
    if guard is not None:
        guard.reset()

def run_daemon(nav: Navigator, settings: Settings, *, max_cycles: int | None = None) -> None:
    nav.reconcile_on_startup()
    cycle = 0
    while True:
        cycle += 1
        _reset_budget(nav)
        try:
            summary = _cycle(nav, settings)
        except Exception as e:  # a dependency blip must not kill the daemon; retry next tick
            summary = {"error": f"{type(e).__name__}: {e}"[:300]}
        print(json.dumps({"cycle": cycle, **summary}), flush=True)
        if max_cycles is not None and cycle >= max_cycles:
            return
        time.sleep(settings.tick_interval_s)

def main(argv: list[str] | None = None) -> int:
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
