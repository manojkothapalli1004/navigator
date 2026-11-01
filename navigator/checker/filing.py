"""Turn checker faults into Navigator proposals against the ACT repo (origin "site-checker")."""
from __future__ import annotations

from navigator.checker.checks import Finding
from navigator.checker.store import FindingStore
from navigator.contracts.proposal import Proposal, compute_origin_hash

CHECKER_ORIGIN = "site-checker"
ACT_REPO = "/Users/Manoj/external-tools/agent-control-tower"


def build_proposal(f: Finding, base_sha: str) -> Proposal:
    title = f"Site checker: {f.check} on {f.page} ({f.viewport})"
    rationale = (f"The site checker found a fault on the live console: {f.detail} "
                 f"(page {f.page}, {f.viewport} view, element {f.element}). Screenshot: {f.screenshot or 'none'}.")
    intended = f"""SELF-CONTAINED SPEC (site checker). Repo: agent-control-tower.
1. FAULT (falsifiable)
- Page: {f.page}   Viewport: {f.viewport} ({'412x915 touch phone' if f.viewport == 'phone' else '1440x900 desktop'})
- Check: {f.check}   Element: {f.element}
- What the checker saw: {f.detail}
- Screenshot: {f.screenshot or 'none'}
2. CHANGE
- Fix it in frontend/ only (frontend/components, frontend/app, frontend/lib); do not touch backend/.
- Add a vitest test under frontend/tests that fails before the fix and passes after.
- Keep the existing look (fonts, colours, spacing scale); phone first.
3. VERIFY
- cd frontend && npx vitest run   (full suite green)
- cd frontend && npx tsc --noEmit -p .
- The checker re-checks this fault on a preview of the branch before merge (fingerprint {f.fingerprint}).
"""
    return Proposal(
        origin=CHECKER_ORIGIN,
        proposal_origin_hash=compute_origin_hash(ACT_REPO, f.fingerprint, [], CHECKER_ORIGIN, f.fingerprint),
        title=title, rationale=rationale, repo=ACT_REPO, base_commit_sha=base_sha,
        intended_changes=intended, affected_files=[],
    )


def file_findings(store: FindingStore, act, base_sha: str, cap: int = 3) -> list[str]:
    """File the oldest unfiled faults (keeping at most `cap` open). Stops at the first ACT error; the
    rest stay unfiled and are retried next run."""
    filed: list[str] = []
    for f in store.to_file(cap):
        try:
            p = act.create_proposal(build_proposal(f, base_sha))
        except Exception:
            break
        store.mark_filed(f.fingerprint, p.id)
        filed.append(p.id)
    return filed
