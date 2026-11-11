"""Approval cards: a gated action the conversation wants to run, waiting for the operator.

A card shows exactly what will run. The operator approves it once (website, Telegram or
terminal) within an hour; the hook then allows that one identical call and the card is used.
Stored in cards.json (0600) so an approval survives a conductor restart."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
import time
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read off a phone
CODE_LEN = 6
TTL_S = 3600
APPROVED_MIN_TTL_S = 600  # an approved card stays consumable at least this long (its helper needs time)
MAX_SUMMARY = 2000
DECLINE_COOLDOWN_S = 600  # a declined action can't raise a fresh card for 10 minutes


DEFAULT_CARD_CAP_USD = 5.0  # a build card whose estimated cost is over this cannot be approved


def card_cap_usd() -> float:
    """CONDUCTOR_CARD_CAP_USD read at call time; unset, blank, not a finite number or <= 0 means the default."""
    try:
        v = float(os.getenv("CONDUCTOR_CARD_CAP_USD", ""))
    except ValueError:
        return DEFAULT_CARD_CAP_USD
    return v if math.isfinite(v) and v > 0 else DEFAULT_CARD_CAP_USD


def _est(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None


class CardError(Exception):
    pass


class Declined(CardError):
    """The operator declined this exact action recently: no new card (2026-11-02 polish)."""

    def __init__(self, minutes_ago: int):
        super().__init__(f"declined {minutes_ago} min ago")
        self.minutes_ago = minutes_ago


def _action(tool_name: str, tool_input: dict) -> dict:
    """What actually runs. A Bash call's description label and timeout don't change the action
    (2026-11-01: re-runs with a new label never matched their approved card). Every other key —
    sandbox flag, background, anything added later — still has to match exactly."""
    if tool_name != "Bash":
        return tool_input
    action = {k: v for k, v in tool_input.items() if k not in _BASH_IGNORED}
    if action.get("run_in_background") is False:
        del action["run_in_background"]  # explicit False == absent; anything else is kept as-is
    return action


_BASH_IGNORED = ("description", "timeout")


def fingerprint(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    # the folder is part of the action: an approved `git merge x` must not run in another repo
    return hashlib.sha256(json.dumps([tool_name, _action(tool_name, tool_input), cwd], sort_keys=True,
                                     default=str).encode()).hexdigest()


def summarize(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    """Everything the operator approves, in full: the card is the only thing they see."""
    if tool_name == "Bash":
        s = str(tool_input.get("command", ""))
        if tool_input.get("dangerouslyDisableSandbox"):
            s += "  [sandbox disabled]"
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
        now = self.clock()
        for card in reversed(self._cards):
            if card["fingerprint"] != fp:
                continue
            if card["state"] == "declined" and now - card.get("decided", 0) < DECLINE_COOLDOWN_S:
                # no retry-until-yes pop-ups; a clock stepping back reads as "just now", never negative
                raise Declined(max(0, int((now - card["decided"]) // 60)))
            if card["state"] == "pending" and self._live(card):
                return dict(card), False
        codes = {c["code"] for c in self._cards}
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        # input + cwd: what the helper that runs an approved card is handed (2026-11-04)
        card = {"id": secrets.token_urlsafe(16), "code": code, "tool": tool_name,
                "summary": summary, "reason": reason, "fingerprint": fp, "input": tool_input, "cwd": cwd,
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
        if state == "approved" and (why := self.over_cap(card)):
            raise CardError(why)  # before any state change; decline is never blocked
        now = self.clock()
        card["state"], card["via"], card["decided"] = state, via, now
        if state == "approved":
            card["expires"] = max(card["expires"], now + APPROVED_MIN_TTL_S)
        self._save()
        return dict(card)

    def over_cap(self, card: dict) -> str | None:
        """The refusal text when the card's estimated cost is over the cap in force now, else None."""
        est, cap = _est(card.get("est_cost_usd")), card_cap_usd()
        if est is None or est <= cap:
            return None
        return (f"estimated cost ${est:.2f} is over the ${cap:.2f} per-card cap; not approved. "
                "Lower the run's cost cap or ask for a smaller task")

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

    def get(self, key: str) -> dict | None:
        try:
            return dict(self._find(key))
        except CardError:
            return None

    def raise_build_card(self, proposal_id: str, project: str, title: str, files: list[str],
                         reason: str, est_cost_usd: float | None = None) -> tuple[dict, bool]:
        """An Approve/Decline card for a build that waits for the operator. One card per proposal, ever:
        an expired or decided card is not raised again. Not an action card: no input, no cwd, and a
        fingerprint that is not a sha256, so consume() can never match it."""
        fp = f"build:{proposal_id}"
        for card in reversed(self._cards):
            if card["fingerprint"] == fp:
                return dict(card), False
        shown = list(files[:BUILD_FILES_SHOWN])
        while True:
            more = len(files) - len(shown)
            summary = (f"Build {proposal_id}: {title}\nproject: {project}\n"
                       f"files: {', '.join(shown) or 'none declared'}" + (f" (+{more} more)" if more else ""))
            if len(summary) <= MAX_SUMMARY or not shown:
                break
            shown.pop()  # drop file names, never the title, project or the count
        est, cap = _est(est_cost_usd), card_cap_usd()
        extra = {}
        if est is not None:  # the cost line sits outside the shrink loop and the final cut
            cost = (f"est. cost: up to ${est:.2f} per run - OVER the ${cap:.2f} card cap, cannot be approved"
                    if est > cap else f"est. cost: up to ${est:.2f} per run (Navigator per-run cap)")
            summary = summary[:MAX_SUMMARY - len(cost) - 1] + "\n" + cost
            extra = {"est_cost_usd": round(est, 2), "cap_usd": cap}
        summary = summary[:MAX_SUMMARY]
        now = self.clock()
        codes = {c["code"] for c in self._cards}
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        card = {"id": secrets.token_urlsafe(16), "code": code, "tool": "build", "summary": summary,
                "reason": reason, "fingerprint": fp, "build": True, "proposal_id": proposal_id,
                "state": "pending", "created": now, "expires": now + self.ttl_s, **extra}
        self._cards.append(card)
        self._save()
        return dict(card), True

    def raise_restart_card(self, label: str, delay_s: int, helpers: list[dict], reason: str) -> tuple[dict, bool]:
        """An Approve/Decline card for restarting a launchd job. Not an action card: no input, no cwd, a
        fingerprint that is not a sha256 (consume() can never match it), and it is never handed to a
        helper; the decide route schedules the restart itself. One pending card per label."""
        from navigator.conductor.restart import LABELS
        if label not in LABELS:
            raise CardError("unknown label")
        fp = f"restart:{label}"
        now = self.clock()
        for card in reversed(self._cards):
            if card["fingerprint"] != fp:
                continue
            if card["state"] == "declined" and now - card.get("decided", 0) < DECLINE_COOLDOWN_S:
                raise Declined(max(0, int((now - card["decided"]) // 60)))
            if card["state"] == "pending" and self._live(card):
                return dict(card), False
        shown = list(helpers)
        while True:
            if helpers:
                names = ", ".join(f"{h['id']} {h.get('title') or ''} {_age(h.get('age') or 0)}" for h in shown)
                more = len(helpers) - len(shown)
                lines = (f"{len(helpers)} helper(s) running or queued; they will be marked LOST and must be "
                         f"re-delegated" + (f": {names}" if shown else ""))
                if more and shown:
                    lines += f" (+{more} more)"
            else:
                lines = "no helpers running"
            summary = (f"Restart {label} (launchd job {LABELS[label]}) in {int(delay_s)}s.\n{lines}\n"
                       "the turn in progress, if any, will be marked interrupted")
            if len(summary) <= MAX_SUMMARY or not shown:
                break
            shown.pop()  # drop helper names, never the label or the count
        summary = summary[:MAX_SUMMARY]
        codes = {c["code"] for c in self._cards}
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        card = {"id": secrets.token_urlsafe(16), "code": code, "tool": "restart", "summary": summary,
                "reason": reason, "fingerprint": fp, "restart": True, "label": label, "delay_s": int(delay_s),
                "state": "pending", "created": now, "expires": now + self.ttl_s}
        self._cards.append(card)
        self._save()
        return dict(card), True


def _age(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 120 else f"{s // 60}m"


BUILD_FILES_SHOWN = 12  # file names listed on a build card; the rest become "(+N more)"
