"""Every fault the checker has seen, keyed by fingerprint, so one fault never becomes two proposals.

States: unfiled (seen, no proposal yet) -> open (proposal filed) -> merged | closed (declined/failed).
A merged fault that shows up again is a regression for the operator, never a new proposal; a closed
one is not filed again either (someone already said no)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from navigator.checker.checks import Finding

MERGED = {"merged", "completed"}
CLOSED = {"declined", "expired", "superseded", "failed", "rolled_back", "aborted"}


@dataclass
class Triage:
    new: list[Finding] = field(default_factory=list)
    still_open: list[Finding] = field(default_factory=list)
    regressions: list[Finding] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new or self.regressions or self.cleared)


class FindingStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def record(self, findings: list[Finding], now: str) -> Triage:
        t = Triage()
        seen = set()
        for f in findings:
            fp = f.fingerprint
            if fp in seen:
                continue
            seen.add(fp)
            entry = self._data.get(fp)
            if entry is None:
                self._data[fp] = {"finding": asdict(f), "state": "unfiled", "proposal_id": None,
                                  "first_seen": now, "last_seen": now}
                t.new.append(f)
                continue
            entry["last_seen"] = now
            entry["finding"] = asdict(f)
            entry.pop("gone", None)
            if entry["state"] == "merged":
                t.regressions.append(f)
            elif entry["state"] in ("unfiled", "open"):
                t.still_open.append(f)
        for fp, entry in list(self._data.items()):
            if fp in seen or entry["state"] not in ("unfiled", "open") or entry.get("gone"):
                continue
            t.cleared.append(fp)
            if entry["state"] == "unfiled":
                del self._data[fp]   # gone before anyone filed it: nothing to track
            else:
                entry["gone"] = True  # reported once; its proposal is still followed until it closes
        self._save()
        return t

    def to_file(self, cap: int = 3) -> list[Finding]:
        """Unfiled faults, oldest first, so that at most `cap` checker proposals are open at once."""
        room = cap - sum(1 for e in self._data.values() if e["state"] == "open")
        if room <= 0:
            return []
        unfiled = sorted((e for e in self._data.values() if e["state"] == "unfiled"), key=lambda e: e["first_seen"])
        return [Finding(**e["finding"]) for e in unfiled[:room]]

    def mark_filed(self, fingerprint: str, proposal_id: str) -> None:
        entry = self._data[fingerprint]
        entry["state"], entry["proposal_id"] = "open", proposal_id
        self._save()

    def fingerprints_for(self, proposal_id: str) -> list[str]:
        """The faults filed under `proposal_id` (any state), sorted."""
        return sorted(fp for fp, e in self._data.items() if e.get("proposal_id") == proposal_id)

    def open_proposals(self) -> dict[str, str]:
        return {fp: e["proposal_id"] for fp, e in self._data.items() if e["state"] == "open"}

    def sync_proposals(self, status_by_id: dict[str, str]) -> None:
        """Follow each filed proposal: merged -> merged, declined/failed -> closed."""
        for entry in self._data.values():
            status = status_by_id.get(entry.get("proposal_id") or "")
            if entry["state"] != "open" or status is None:
                continue
            if status in MERGED:
                entry["state"] = "merged"
            elif status in CLOSED:
                entry["state"] = "closed"
        self._save()
