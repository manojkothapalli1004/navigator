"""python -m navigator.checker run [--base URL] [--file]
python -m navigator.checker verify <branch> --fingerprint FP [--fingerprint FP ...]

run     checks the LIVE console, records faults, tells Telegram only when something changed.
        Exit 0 clean, 1 faults found, 2 could not run. --file files new faults as Navigator
        proposals (off by default until Navigator can verify frontend builds).
verify  builds <branch> into a preview on :3001, re-checks only the given faults' pages and
        writes before/after screenshot pairs; exit 0 when every fault is gone."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from navigator.checker import browse as browse_mod
from navigator.checker import preview
from navigator.checker.checks import Finding, check_page
from navigator.checker.filing import ACT_REPO, file_findings
from navigator.checker.store import FindingStore

LIVE = "http://127.0.0.1:3000"
ROOT = Path("/Users/Manoj/calwbot/logs/navigator/checker")
SHOWN = 5


def _act_base_sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ACT_REPO, capture_output=True, text=True).stdout.strip()


def _statuses(act, store: FindingStore) -> dict[str, str]:
    """Combined status per filed proposal: its latest run's status when it has runs, else the proposal's."""
    out: dict[str, str] = {}
    for pid in store.open_proposals().values():
        try:
            runs = act.list_runs_for_proposal(pid)
            if runs:
                # the client promises no order: the latest run is the one that started last
                latest = max(runs, key=lambda r: str(getattr(r, "started_at", "") or ""))
                out[pid] = getattr(latest.status, "value", str(latest.status))
                continue
            prop = act.get_proposal(pid)
            if prop is not None:
                out[pid] = getattr(prop.status, "value", str(prop.status))
        except Exception:
            continue
    return out


def _message(triage, filed: list[str], run_dir: Path) -> str:
    lines = ["Site checker:"]
    for label, items in (("new", triage.new), ("came back after a fix", triage.regressions)):
        if items:
            lines.append(f"{len(items)} {label}:")
            lines += [f"- {f.check} on {f.page} ({f.viewport}): {f.detail}" for f in items[:SHOWN]]
            if len(items) > SHOWN:
                lines.append(f"- ...and {len(items) - SHOWN} more")
    if triage.cleared:
        lines.append(f"{len(triage.cleared)} fixed or gone")
    if filed:
        lines.append(f"filed {len(filed)} fix proposal(s)")
    lines.append(f"screenshots: {run_dir}")
    return "\n".join(lines)


def cmd_run(args, *, browse_fn, act, notifier, root: Path, base_sha) -> int:
    base = args[args.index("--base") + 1] if "--base" in args else LIVE
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        snaps = browse_fn(base, run_dir)
    except browse_mod.ConsoleUnreachable as e:
        notifier.send(f"Site checker: console unreachable ({e})")
        return 2
    except browse_mod.BrowserUnavailable as e:
        notifier.send(f"Site checker: could not start Chrome ({e})")
        return 2
    except Exception as e:  # anything else: say so instead of dying silently under launchd
        notifier.send(f"Site checker: run failed ({type(e).__name__}: {e})")
        return 2
    now = time.time()
    findings: list[Finding] = [f for snap, shot in snaps for f in check_page(snap, now, screenshot=shot)]
    store = FindingStore(root / "store.json")
    store.sync_proposals(_statuses(act, store))
    triage = store.record(findings, datetime.now(timezone.utc).isoformat())
    filed = file_findings(store, act, base_sha()) if "--file" in args else []
    (run_dir / "summary.json").write_text(json.dumps({
        "base": base, "pages": len({s["page"] for s, _ in snaps}), "findings": [asdict(f) for f in findings],
        "new": [asdict(f) for f in triage.new], "regressions": [asdict(f) for f in triage.regressions],
        "cleared": triage.cleared, "filed": filed,
    }, indent=1))
    if triage.changed or filed:
        notifier.send(_message(triage, filed, run_dir))
    return 1 if findings else 0


def cmd_verify(args, *, root: Path) -> int:
    branch = args[0]
    fps = [args[i + 1] for i, a in enumerate(args) if a == "--fingerprint"]
    store = FindingStore(root / "store.json")
    wanted = {fp: Finding(**e["finding"]) for fp, e in store._data.items() if fp in fps}
    if not wanted:
        print("no known fault for those fingerprints", file=sys.stderr)
        return 2
    out = root / f"verify-{branch.replace('/', '_')}"
    out.mkdir(parents=True, exist_ok=True)
    work = root / "preview-work"
    base = preview.build(branch, work)
    ok = True
    try:
        with browse_mod.open_browser() as b:
            for fp, f in wanted.items():
                snap, shot = browse_mod.snapshot(b, base, f.page, f.viewport, out)
                after = Path(shot).rename(out / f"{fp}__after.png")
                if f.screenshot and Path(f.screenshot).exists():
                    shutil.copy(f.screenshot, out / f"{fp}__before.png")
                still = [g for g in check_page(snap, time.time()) if g.fingerprint == fp]
                print(f"{'FAIL' if still else 'PASS'} {fp} {f.check} on {f.page} ({f.viewport}) -> {after}")
                ok = ok and not still
    finally:
        preview.stop()
        shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


def main(argv=None, *, browse_fn=None, act=None, notifier=None, root: Path = ROOT, base_sha=_act_base_sha) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("run", "verify"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "verify":
        return cmd_verify(argv[1:], root=root)
    if act is None:
        from navigator.clients.act_client import HTTPACTClient
        act = HTTPACTClient("http://127.0.0.1:8090")
    if notifier is None:
        from navigator.observability.notify import make_notifier
        notifier = make_notifier()
    return cmd_run(argv[1:], browse_fn=browse_fn or browse_mod.browse, act=act, notifier=notifier,
                   root=root, base_sha=base_sha)


if __name__ == "__main__":
    sys.exit(main())
