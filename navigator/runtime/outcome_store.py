import json
from pathlib import Path
from navigator.contracts.outcome import Outcome

# lesson text recorded when the operator declines a run at the merge gate (the
# executor writes this generic form; the planner snapshot adds the title so the
# planner can tell WHAT was declined instead of re-filing near-duplicates)
DECLINED_LESSON = "operator declined the change at the merge gate"
_TITLE_CHARS = 120


def declined_lesson(title: str | None = None) -> str:
    """The merge-declined lesson, naming the proposal title when one is known.
    The title is flattened to one line (newlines -> spaces) and capped at 120 chars.
    With title=None the exact legacy text is returned."""
    if title is None:
        return DECLINED_LESSON
    flat = " ".join(str(title).split())[:_TITLE_CHARS]
    return f"{DECLINED_LESSON}: '{flat}' - do not re-propose without addressing why"


def title_lessons(lessons: list[str], title: str | None) -> list[str]:
    """Return `lessons` with the generic declined lesson replaced by the titled form.
    Other lessons (failed, quota-deferred, stale) pass through unchanged."""
    if title is None:
        return list(lessons)
    return [declined_lesson(title) if l == DECLINED_LESSON else l for l in lessons]


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
