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


def fingerprint(tool_name: str, tool_input: dict) -> str:
    return hashlib.sha256(json.dumps([tool_name, tool_input], sort_keys=True, default=str)
                          .encode()).hexdigest()


def summarize(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        s = str(tool_input.get("command", ""))
    elif tool_name in ("Edit", "MultiEdit"):
        s = f"{tool_name} {tool_input.get('file_path')}: {tool_input.get('old_string')!r} -> {tool_input.get('new_string')!r}"
        if tool_name == "MultiEdit":
            s = f"MultiEdit {tool_input.get('file_path')}: {len(tool_input.get('edits') or [])} edits"
    elif tool_name in ("Write", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        body = tool_input.get("content") or tool_input.get("new_source") or ""
        s = f"{tool_name} {path} ({len(str(body))} chars)"
    else:
        s = f"{tool_name} {json.dumps(tool_input, sort_keys=True, default=str)}"
    if len(s) > MAX_SUMMARY:
        s = s[:MAX_SUMMARY] + f" … [truncated, {len(s)} chars in full]"
    return s


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

    def raise_card(self, tool_name: str, tool_input: dict, reason: str) -> tuple[dict, bool]:
        fp = fingerprint(tool_name, tool_input)
        for card in reversed(self._cards):
            if card["fingerprint"] == fp and card["state"] == "pending" and self._live(card):
                return dict(card), False
        now = self.clock()
        codes = {c["code"] for c in self._cards}
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        card = {"id": secrets.token_urlsafe(16), "code": code, "tool": tool_name,
                "summary": summarize(tool_name, tool_input), "reason": reason, "fingerprint": fp,
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

    def consume(self, tool_name: str, tool_input: dict) -> dict | None:
        fp = fingerprint(tool_name, tool_input)
        for card in reversed(self._cards):
            if card["fingerprint"] == fp and card["state"] == "approved" and self._live(card):
                card["state"] = "used"
                self._save()
                return dict(card)
        return None

    def pending(self) -> list[dict]:
        return [dict(c) for c in self._cards if c["state"] == "pending" and self._live(c)]
