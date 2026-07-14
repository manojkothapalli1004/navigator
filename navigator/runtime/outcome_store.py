import json
from pathlib import Path
from navigator.contracts.outcome import Outcome

class OutcomeStore:
    """Append-only JSONL persistence for run Outcomes.

    One JSON object per line. Writes only ever append; a process that dies
    mid-write leaves a partial trailing line, which readers skip rather than
    raise on. Lessons still go to ORC memory independently — this is the local
    durable trail the planner can consult.
    """
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, outcome: Outcome) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(outcome.model_dump(), separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _read_all(self) -> list[Outcome]:
        if not self.path.exists():
            return []
        outcomes: list[Outcome] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                outcomes.append(Outcome(**json.loads(line)))
            except (ValueError, TypeError):
                continue  # partial/corrupt line (e.g. crash mid-write): skip
        return outcomes

    def latest(self, n: int) -> list[Outcome]:
        return list(reversed(self._read_all()))[:n]

    def for_proposal(self, proposal_id: str) -> list[Outcome]:
        return [o for o in self._read_all() if o.proposal_id == proposal_id]
