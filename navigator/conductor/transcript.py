"""C5: read what happened in a terminal `claude --resume` session from its transcript file, so
the web/Telegram history shows the terminal's exchanges. Only operator text and assistant text
are kept — tool calls, tool results, meta and sidechain records are not."""
from __future__ import annotations

import json
import re
from pathlib import Path

from navigator.conductor.runner import CWD


def transcript_path(session_id: str, cwd: str = CWD, home: Path | None = None) -> Path:
    slug = re.sub(r"[/.]", "-", cwd)
    return Path(home if home is not None else Path.home()) / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def size(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "").strip() for b in content
                           if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip())
    return ""


def read_since(path: Path, offset: int) -> list[dict]:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue  # a line half-written when we read
        if not isinstance(r, dict) or r.get("isMeta") or r.get("isSidechain"):
            continue
        kind = r.get("type")
        if kind not in ("user", "assistant"):
            continue
        text = _text((r.get("message") or {}).get("content"))
        if not text:
            continue
        if kind == "assistant" and out and out[-1]["kind"] == "assistant":
            out[-1]["text"] += "\n\n" + text  # one reply split across tool calls
        else:
            out.append({"kind": kind, "text": text})
    return out
