"""Approval cards: a gated action the conversation wants to run, waiting for the operator.

A card shows exactly what will run. The operator approves it once (website, Telegram or
terminal) within an hour; the hook then allows that one identical call and the card is used.
Stored in cards.json (0600) so an approval survives a conductor restart."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read off a phone
CODE_LEN = 6
TTL_S = 3600
MAX_SUMMARY = 2000


class CardError(Exception):
    pass


def _action(tool_name: str, tool_input: dict) -> dict:
    """What actually runs. A Bash call's description label and timeout don't change the action
    (2026-11-01: re-runs with a new label never matched their approved card); background does."""
    if tool_name == "Bash":
        return {"command": tool_input.get("command"),
                "run_in_background": bool(tool_input.get("run_in_background"))}
    return tool_input


def fingerprint(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    # the folder is part of the action: an approved `git merge x` must not run in another repo
    return hashlib.sha256(json.dumps([tool_name, _action(tool_name, tool_input), cwd], sort_keys=True,
                                     default=str).encode()).hexdigest()


def summarize(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    """Everything the operator approves, in full: the card is the only thing they see."""
    if tool_name == "Bash":
        s = str(tool_input.get("command", ""))
    elif tool_name == "Edit":
        s = f"Edit {tool_input.get('file_path')}: {tool_input.get('old_string')!r} -> {tool_input.get('new_string')!r}"
    elif tool_name == "MultiEdit":
        edits = "; ".join(f"{e.get('old_string')!r} -> {e.get('new_string')!r}"
                          for e in (tool_input.get("edits") or []) if isinstance(e, dict))
        s = f"MultiEdit {tool_input.get('file_path')}: {edits}"
    elif tool_name in ("Write", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        body = tool_input.get("content") or tool_input.get("new_source") or ""
        s = f"{tool_name} {path}:\n{body}"
    else:
        s = f"{tool_name} {json.dumps(tool_input, sort_keys=True, default=str)}"
    return f"in {cwd}: {s}" if cwd else s


class Cards:
    def __init__(self, path: Path, ttl_s: int = TTL_S, clock=time.time):
        self.path, self.ttl_s, self.clock = Path(path), ttl_s, clock
        self._cards: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cards-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self._cards[-500:], fh)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._cards = self._cards[-500:]

    def _live(self, card: dict) -> bool:
        return self.clock() < card["expires"]

    def _find(self, key: str) -> dict:
        k = key.strip()
        for card in reversed(self._cards):
            if card["id"] == k or card["code"] == k.upper():
                return card
        raise CardError("not found")

    def raise_card(self, tool_name: str, tool_input: dict, reason: str,
                   cwd: str | None = None) -> tuple[dict, bool]:
        summary = summarize(tool_name, tool_input, cwd)
        if len(summary) > MAX_SUMMARY:
            # a truncated card could hide the gated part, and the retry could never match it
            raise CardError("too long")
        fp = fingerprint(tool_name, tool_input, cwd)
        for card in reversed(self._cards):
            if card["fingerprint"] == fp and card["state"] == "pending" and self._live(card):
                return dict(card), False
        now = self.clock()
        codes = {c["code"] for c in self._cards}
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        card = {"id": secrets.token_urlsafe(16), "code": code, "tool": tool_name,
                "summary": summary, "reason": reason, "fingerprint": fp,
                "state": "pending", "created": now, "expires": now + self.ttl_s}
        self._cards.append(card)
        self._save()
        return dict(card), True

    def _decide(self, key: str, via: str, state: str) -> dict:
        card = self._find(key)
        if card["state"] != "pending":
            raise CardError(f"already {card['state']}")
        if not self._live(card):
            raise CardError("expired")
        card["state"], card["via"], card["decided"] = state, via, self.clock()
        self._save()
        return dict(card)

    def approve(self, key: str, via: str) -> dict:
        return self._decide(key, via, "approved")

    def decline(self, key: str, via: str) -> dict:
        return self._decide(key, via, "declined")

    def consume(self, tool_name: str, tool_input: dict, cwd: str | None = None) -> dict | None:
        fp = fingerprint(tool_name, tool_input, cwd)
        for card in reversed(self._cards):
            if card["fingerprint"] == fp and card["state"] == "approved" and self._live(card):
                card["state"] = "used"
                self._save()
                return dict(card)
        return None

    def pending(self) -> list[dict]:
        return [dict(c) for c in self._cards if c["state"] == "pending" and self._live(c)]
