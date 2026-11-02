"""python -m navigator.curator run [--file]

run     scans skills, writes a report under logs/navigator/curator/<stamp>/report.json, tells Telegram
        only when the findings changed. Exit 0 clean, 1 findings, 2 could not run.
        --file also files ONE consolidation proposal (review-gated: nothing is built until the
        operator approves it) when the findings changed since the last filing.
The curator never edits a skill."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.curator import scan as cscan

SKILLS_REPO = Path("/Users/Manoj/calwbot/calwbot-skills")
OWN_ROOTS = [SKILLS_REPO / "skills", Path.home() / ".claude" / "skills"]
PLUGIN_ROOTS = [Path.home() / ".claude" / "plugins" / "cache"]
ROOT = Path("/Users/Manoj/calwbot/logs/navigator/curator")


def _skills_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=SKILLS_REPO, capture_output=True, text=True).stdout.strip()


def build_proposal(findings: list[cscan.Finding], repo_root: Path, base_sha: str) -> Proposal:
    lines = [f"- {f.kind}: {', '.join(f.skills)} — {f.detail}" for f in findings]
    affected = sorted({str(Path(p).resolve().relative_to(Path(repo_root).resolve()))
                       for f in findings for p in f.paths
                       if Path(p).resolve().is_relative_to(Path(repo_root).resolve())})
    intent = "consolidate skills: " + ",".join(sorted(f.fingerprint for f in findings))
    return Proposal(
        origin="curator",
        proposal_origin_hash=compute_origin_hash("calwbot-skills", intent, affected, "curator", None),
        title=f"Skill consolidation: {len(findings)} findings",
        rationale="The curator scanned the skills and found:\n" + "\n".join(lines),
        repo="calwbot-skills", base_commit_sha=base_sha,
        intended_changes=("Consolidate the skills listed below — merge duplicates/overlaps into one skill, fix or "
                          "remove stale paths, add missing descriptions. Only skills under this repo may be "
                          "edited; installed plugin skills are left as they are. The curator itself never edits "
                          "skills: this proposal is the review step.\n" + "\n".join(lines)),
        affected_files=affected)


def cmd_run(args, *, own_roots, plugin_roots, root: Path, act, notifier, repo_root: Path, base_sha,
            local_prefixes=cscan.LOCAL_PREFIXES) -> int:
    try:
        found = cscan.find(cscan.scan(own_roots, plugin_roots), local_prefixes)
    except Exception as e:
        notifier.send(f"Curator: run failed ({type(e).__name__})")
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    fps = sorted(f.fingerprint for f in found)
    changed = fps != state.get("last", [])
    filed = None
    if found and "--file" in args and fps != state.get("filed", []):
        try:
            filed = act.create_proposal(build_proposal(found, repo_root, base_sha())).id
            state["filed"] = fps
        except Exception as e:
            notifier.send(f"Curator: filing failed ({type(e).__name__})")
    (run_dir / "report.json").write_text(json.dumps(
        {"findings": [asdict(f) | {"paths": f.paths} for f in found], "filed": filed}, indent=1, default=str))
    state["last"] = fps
    state_path.write_text(json.dumps(state))
    if found and (changed or filed):
        kinds = {}
        for f in found:
            kinds[f.kind] = kinds.get(f.kind, 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        notifier.send(f"Curator: {len(found)} findings ({summary})" + (f"; filed {filed}" if filed else "")
                      + f" — {run_dir / 'report.json'}")
    return 1 if found else 0


def main(argv=None, *, own_roots=None, plugin_roots=None, root: Path = ROOT, act=None, notifier=None,
         repo_root: Path = SKILLS_REPO, base_sha=_skills_head, local_prefixes=cscan.LOCAL_PREFIXES) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "run":
        print(__doc__, file=sys.stderr)
        return 2
    if act is None:
        from navigator.clients.act_client import HTTPACTClient
        act = HTTPACTClient("http://127.0.0.1:8090")
    if notifier is None:
        from navigator.observability.notify import make_notifier
        notifier = make_notifier()
    return cmd_run(argv[1:], own_roots=own_roots or OWN_ROOTS, plugin_roots=plugin_roots or PLUGIN_ROOTS,
                   root=root, act=act, notifier=notifier, repo_root=repo_root, base_sha=base_sha,
                   local_prefixes=local_prefixes)


if __name__ == "__main__":
    sys.exit(main())
