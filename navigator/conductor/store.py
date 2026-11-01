"""The conversation's durable state: the Agent SDK session id (state.json) and the display
history every window renders (history.jsonl, one entry per line)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from navigator.ids import new_id


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._state = self.root / "state.json"
        self._history = self.root / "history.jsonl"

    @property
    def session_id(self) -> str | None:
        try:
            return json.loads(self._state.read_text()).get("session_id")
        except (OSError, ValueError):
            return None

    def set_session_id(self, sid: str | None) -> None:
        tmp = self._state.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"session_id": sid}))
        os.replace(tmp, self._state)

    def append(self, entry: dict) -> dict:
        entry = {"id": new_id("msg"), "ts": datetime.now(timezone.utc).isoformat(), **entry}
        with open(self._history, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def history(self, after: str | None = None, limit: int = 200) -> list[dict]:
        entries = []
        try:
            lines = self._history.read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a line half-written when the service died
        if after is not None:
            idx = next((i for i, e in enumerate(entries) if e.get("id") == after), None)
            if idx is not None:
                entries = entries[idx + 1:]
        return entries[-limit:]
