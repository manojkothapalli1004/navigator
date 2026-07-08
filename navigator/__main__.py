import argparse
import json
import subprocess
import sys
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

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="navigator")
    parser.add_argument("--once", action="store_true", help="run a single perceive/plan/poll cycle")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("only --once is supported; the interval daemon arrives in v1")

    settings = Settings.from_env()
    nav = build_navigator(settings)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=settings.repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    nav.reconcile_on_startup()
    created = nav.periodic_tick(base_commit_sha=sha)
    runs = nav.approval_poll()
    print(json.dumps({"mode": settings.mode, "driver": settings.driver,
                      "proposals_created": len(created), "runs_executed": len(runs)}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
