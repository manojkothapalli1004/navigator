"""Proposal decisions typed in Telegram (operator 2026-10-03: approve from Telegram).

The manojtest bot is the bot's only getUpdates consumer; it hands every message to
handle_operator_message, which applies the inbox's checks (operator's private chat,
not forwarded, under an hour old) and then handles:
  'status' / 'more' / 'all'      -> the top 3 in triage order (runtime/triage.py), the
                                    next 3, or the whole numbered list
  'approve <t> ...' / 'decline <t> ...'
                                 -> <t> is a number from that list, a range '4-33',
                                    a short name ('N1') or a full proposal id
  'help'                         -> the command list
  merge/reject forms             -> telegram_inbox.apply_commands, unchanged
Targets resolve against one snapshot of the list; if any target is unknown or
ambiguous nothing is changed. Anything else returns False (ordinary chat).
"""
import os
import re
import time
from dataclasses import dataclass

from navigator.contracts.enums import ProposalStatus
from navigator.observability.logging import get_logger
from navigator.runtime import triage
from navigator.observability.telegram_inbox import (TelegramInbox, apply_commands,
                                                    is_operator_message, parse_command,
                                                    short_name)

log = get_logger("navigator.telegram_proposals")

MAX_TARGETS = 64
TELEGRAM_CHUNK = 3900
PROPOSAL_ID = re.compile(r"proposal-[0-9A-HJKMNP-TV-Z]{26}")
TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RANGE = re.compile(r"(\d{1,4})-(\d{1,4})")
HELP = ("Navigator commands:\n"
        "status - the top 3 proposals to decide / more - the next 3 / all - every one\n"
        "approve 1 3 / approve N1 / decline 4-33 - decide proposals\n"
        "merge / merge N5b / reject N5b - runs waiting at the merge gate")


@dataclass(frozen=True)
class ProposalCommand:
    action: str  # "status" | "more" | "all" | "help" | "approve" | "decline"
    targets: tuple[str, ...] = ()


def parse_proposal_command(text) -> ProposalCommand | None:
    if not isinstance(text, str) or "\n" in text or "\r" in text:
        return None
    parts = text.replace(",", " ").split()
    if not parts:
        return None
    word = parts[0].lower().lstrip("/")
    args = tuple(parts[1:])
    if word in ("status", "help", "more", "all"):
        return ProposalCommand(word) if not args else None
    if word not in ("approve", "decline") or not args or len(args) > MAX_TARGETS:
        return None
    if not all(TARGET.fullmatch(a) and not a.lower().startswith("run-") for a in args):
        return None
    return ProposalCommand(word, args)


def triage_path() -> str:
    return os.getenv("NAVIGATOR_TRIAGE_PATH") or triage.DEFAULT_TRIAGE_PATH


def numbered_proposals(act, path: str | None = None) -> list:
    """Proposals awaiting a decision in triage order; the numbers the operator types."""
    return [p for p, _ in _order(act, path)]


def _order(act, path: str | None) -> list[tuple]:
    return triage.ordered(act.list_proposals(ProposalStatus.proposed), path or triage_path())


def _chunks(lines: list[str]) -> list[str]:
    out, cur = [], ""
    for line in lines:
        line = line[:TELEGRAM_CHUNK]
        if cur and len(cur) + len(line) + 1 > TELEGRAM_CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _page(act, notifier, path: str | None, more: bool) -> None:
    path = path or triage_path()
    order = _order(act, path)
    start = 0
    if more:
        start = int(triage.load(path).get("cursor") or 0) + triage.TOP_N
    notifier.send(triage.format_top(order, start=start))
    triage.set_cursor(path, start)


def _all(act, notifier, path: str | None) -> None:
    props = numbered_proposals(act, path)
    lines = [f"Navigator: {len(props)} proposal(s) waiting for a decision"]
    lines += [f"{i}. {p.title}" for i, p in enumerate(props, 1)]
    for text in _chunks(lines):
        notifier.send(text)


def _resolve(act, targets, path: str | None) -> tuple[list, str | None]:
    """All targets against one snapshot -> (proposals, None), or ([], reason)."""
    props = numbered_proposals(act, path)
    picked: dict[str, object] = {}
    for t in targets:
        m = RANGE.fullmatch(t)
        if t.isdigit() or m:
            lo, hi = (int(t), int(t)) if t.isdigit() else (int(m[1]), int(m[2]))
            if lo < 1 or hi < lo or hi > len(props):
                return [], f"{t} is not in the list of {len(props)}"
            for p in props[lo - 1:hi]:
                picked[p.id] = p
        elif PROPOSAL_ID.fullmatch(t):
            p = next((p for p in props if p.id == t), None)
            if p is None:
                return [], f"{t} is not waiting for a decision"
            picked[p.id] = p
        else:
            hits = [p for p in props if short_name(p.title).lower() == t.lower()]
            if len(hits) != 1:
                return [], (f"no proposal named {t!r}" if not hits
                            else f"{len(hits)} proposals named {t!r} - use numbers")
            picked[hits[0].id] = hits[0]
    return list(picked.values()), None


def _decide(act, cmd: ProposalCommand, notifier, path: str | None) -> None:
    props, reason = _resolve(act, cmd.targets, path)
    if reason is not None:
        notifier.send(f"Navigator: {cmd.action} not applied, nothing changed - {reason}")
        return
    status = ProposalStatus.approved if cmd.action == "approve" else ProposalStatus.declined
    for p in props:
        act.set_proposal_status(p.id, status)
    lines = [f"Navigator: {status.value} {len(props)}"] + [f"- {p.title}" for p in props]
    if status == ProposalStatus.approved:
        lines.append("builds start on the next cycle")
    lines.append("send 'status' for the new top 3")
    for text in _chunks(lines):
        notifier.send(text)


def handle_operator_message(msg: dict, act, notifier, chat_id: str,
                            now: float | None = None, triage_file: str | None = None) -> bool:
    """True when the message was a Navigator command (handled or refused with a
    reply); False for other senders and ordinary chat."""
    if not isinstance(msg, dict) or not is_operator_message(msg, chat_id):
        return False
    text = msg.get("text")
    merge_cmd = parse_command(text, 0)
    prop_cmd = None if merge_cmd else parse_proposal_command(text)
    if merge_cmd is None and prop_cmd is None:
        return False
    ignored = TelegramInbox._ignored_reason(msg, time.time() if now is None else now)
    if ignored is not None:
        notifier.send(f"Navigator: command ignored ({ignored}) - send it again")
        return True
    try:
        if merge_cmd is not None:
            apply_commands(act, [merge_cmd], notifier)
        elif prop_cmd.action in ("status", "more"):
            _page(act, notifier, triage_file, more=prop_cmd.action == "more")
        elif prop_cmd.action == "all":
            _all(act, notifier, triage_file)
        elif prop_cmd.action == "help":
            notifier.send(HELP)
        else:
            _decide(act, prop_cmd, notifier, triage_file)
    except Exception as e:
        name = type(e).__name__
        log.warning("telegram.proposal_command_failed", error=name)
        try:
            notifier.send(f"Navigator: could not apply that command: {name} - re-send it")
        except Exception:
            pass
    return True
