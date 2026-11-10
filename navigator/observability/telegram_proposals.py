"""Proposal decisions typed in Telegram (operator 2026-10-03: approve from Telegram).

handle_operator_message is the single handler behind the always-on reader (N11,
telegram_reader.py): the reader long-polls getUpdates (TelegramInbox.poll_updates)
and hands every operator message here in the same iteration, together with its
Telegram update_id, so replies arrive at once instead of once per daemon cycle. The
handler applies the inbox's checks (operator's private chat, not forwarded, under an
hour old) and then handles:
  'status' / 'more' / 'all'      -> the top 3 in triage order (runtime/triage.py), the
                                    next 3, or the whole numbered list
  'approve <t> ...' / 'decline <t> ...'
                                 -> <t> is a number from that list, a range '4-33',
                                    a short name ('N1') or a full proposal id;
                                    the proposal status is set in ACT and the
                                    builder starts on the next daemon cycle
  'help'                         -> the command list
  'decisions'                    -> everything waiting on the operator: audited merges,
                                    proposals, tracked manual steps (decisions.py; read
                                    only, the daemon tick owns the nudge/digest state)
  'run <id>' / 'show <id>'       -> run (or print) the ONE pinned script of a tracked
                                    manual step (actions.py: pinned path + sha256, no
                                    arguments, nothing typed reaches the script)
  merge/reject forms             -> telegram_inbox.apply_commands, unchanged; the
                                    run.telegram_decision event carries the update_id
                                    of the originating message (0 only when the
                                    caller has none)
  'fix' / 'fix <name|run-id>'    -> telegram_inbox.apply_commands too (B4): one rebuild
                                    grant for a run declined at the merge gate; a name
                                    that is no declined run's short name is ordinary
                                    chat (False), like a reasoned reject of a stranger
Numbers refer to the list the operator last received (status/more/all or the daemon's
top-3 push), stored as triage.json "shown" (N13): the live triage order may change
between that message and the reply, so a number never decides a proposal the operator
did not see under it. Short names and full ids resolve against the live proposed list.
Targets resolve against one snapshot of the list; if any target is unknown, decided
meanwhile or ambiguous nothing is changed. Anything else returns False (ordinary chat). A command
whose handling raises is answered with the exception class name only (N11c: never an
exception's text, which could carry the token-bearing URL); its offset is already
persisted, so the operator is told to re-send it.
"""
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from navigator.contracts.enums import ProposalStatus
from navigator.observability import actions, decisions
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
        "merge / merge N5b / reject N5b [reason...] - runs waiting at the merge gate\n"
        "merge all / merge all except N5b,S3a - approve every audited tier A/B run at the "
        "gate (tier C needs 'merge <name>')\n"
        "fix N5b - rebuild a declined run with its reason\n"
        "decisions - everything waiting on you (merges, proposals, manual steps)\n"
        "run <id> / show <id> - run or read a pinned manual-step script")


@dataclass(frozen=True)
class ProposalCommand:
    action: str  # "status" | "more" | "all" | "help" | "decisions" | "approve" | "decline"
                 # | "run" | "show" (targets = (manual item id,))
    targets: tuple[str, ...] = ()


def parse_proposal_command(text) -> ProposalCommand | None:
    if not isinstance(text, str) or "\n" in text or "\r" in text:
        return None
    parts = text.replace(",", " ").split()
    if not parts:
        return None
    word = parts[0].lower().lstrip("/")
    args = tuple(parts[1:])
    if word in ("status", "help", "more", "all", "decisions"):
        return ProposalCommand(word) if not args else None
    if word in ("run", "show"):
        # exactly one manual item id; nothing else from the message reaches actions
        if len(args) != 1 or not decisions.MANUAL_ID.fullmatch(args[0]):
            return None
        return ProposalCommand(word, args)
    if word not in ("approve", "decline") or not args or len(args) > MAX_TARGETS:
        return None
    if not all(TARGET.fullmatch(a) and not a.lower().startswith("run-") for a in args):
        return None
    return ProposalCommand(word, args)


def triage_path() -> str:
    return os.getenv("NAVIGATOR_TRIAGE_PATH") or triage.DEFAULT_TRIAGE_PATH


def numbered_proposals(act, path: str | None = None) -> list:
    """Proposals awaiting a decision in the live triage order. Not what typed numbers
    resolve against (that is the stored snapshot, see _resolve); kept for callers that
    want the current order."""
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


def _open_ids(act) -> set[str]:
    return {p.id for p in act.list_proposals(ProposalStatus.proposed)}


def _empty_list_shown(state: dict) -> bool:
    """True after a 'status'/'all' that found nothing to decide (a valid, empty list)."""
    shown = state.get("shown")
    return isinstance(shown, dict) and shown.get("entries") == []


def _no_list_reason(state: dict) -> str:
    return ("nothing waiting for a decision" if _empty_list_shown(state)
            else "no numbered list yet - send 'status' or 'all' first")


def _page(act, notifier, path: str | None, more: bool) -> None:
    path = path or triage_path()
    if not more:
        order = _order(act, path)
        state = triage.set_shown(path, order, cursor=0)
        notifier.send(triage.format_shown(triage.shown_entries(state),
                                          {p.id for p, _ in order}, start=0))
        return
    open_ids = _open_ids(act)
    seen: dict = {}

    def mutate(s):
        # one locked read-modify-write: the cursor continues the stored snapshot only
        # when it was taken on it (cursor_for), otherwise paging restarts (a daemon push
        # replaced the list meanwhile); the new cursor is tied to that snapshot
        entries = triage.shown_entries(s)
        shown = s.get("shown")
        ts = shown.get("ts_epoch") if isinstance(shown, dict) else None
        same = ts is not None and s.get("cursor_for") == ts
        start = (triage.cursor_of(s) if same else 0) + triage.TOP_N
        seen.update(state=s, entries=entries, start=start)
        if not entries:
            return s
        return {**s, "cursor": start, "cursor_for": ts}

    triage.update(path, mutate, rewind_cursor=True)
    if not seen["entries"] and not _empty_list_shown(seen["state"]):
        notifier.send("Navigator: no list shown yet - send 'status' first")
        return
    notifier.send(triage.format_shown(seen["entries"], open_ids, start=seen["start"]))


def _all(act, notifier, path: str | None) -> None:
    path = path or triage_path()
    order = _order(act, path)
    triage.set_shown(path, order, cursor=0)
    props = [p for p, _ in order]
    lines = [f"Navigator: {len(props)} proposal(s) waiting for a decision"]
    lines += [f"{i}. {p.title}" for i, p in enumerate(props, 1)]
    for text in _chunks(lines):
        notifier.send(text)


def _resolve(act, targets, path: str | None) -> tuple[list, str | None]:
    """All targets against one snapshot -> (proposals, None), or ([], reason). Numbers
    and ranges index the list the operator last saw (triage.json 'shown'); short names
    and full ids the live proposed list. Any bad target: nothing is changed."""
    live = act.list_proposals(ProposalStatus.proposed)
    by_id = {p.id: p for p in live}
    state = triage.load(path or triage_path())
    entries = triage.shown_entries(state)
    picked: dict[str, object] = {}
    for t in targets:
        m = RANGE.fullmatch(t)
        if t.isdigit() or m:
            if not entries:
                return [], _no_list_reason(state)
            lo, hi = (int(t), int(t)) if t.isdigit() else (int(m[1]), int(m[2]))
            if lo < 1 or hi < lo or hi > len(entries):
                return [], f"{t} is not in the list of {len(entries)}"
            for k in range(lo, hi + 1):
                e = entries[k - 1]
                p = by_id.get(e["id"])
                if p is None:
                    title = str(e.get("title") or "")[:60]
                    return [], (f"{k}. {title} is no longer waiting for a decision - "
                                "send 'status'")
                picked[p.id] = p
        elif PROPOSAL_ID.fullmatch(t):
            p = by_id.get(t)
            if p is None:
                return [], f"{t} is not waiting for a decision"
            picked[p.id] = p
        else:
            hits = [p for p in live if short_name(p.title).lower() == t.lower()]
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
                            now: float | None = None, triage_file: str | None = None,
                            update_id: int = 0) -> bool:
    """True when the message was a Navigator command (handled or refused with a
    reply); False for other senders and ordinary chat. A reasoned reject whose target
    is not a waiting run's name ('reject this one please') is ordinary chat: False, no
    reply, so the bot forwards it. update_id is the Telegram update that delivered
    msg; merge/reject decisions record it in their event."""
    if not isinstance(msg, dict) or not is_operator_message(msg, chat_id):
        return False
    text = msg.get("text")
    merge_cmd = parse_command(text, update_id)
    prop_cmd = None if merge_cmd else parse_proposal_command(text)
    if merge_cmd is None and prop_cmd is None:
        return False
    ignored = TelegramInbox._ignored_reason(msg, time.time() if now is None else now)
    if ignored is not None:
        notifier.send(f"Navigator: command ignored ({ignored}) - send it again")
        return True
    try:
        if merge_cmd is not None:
            results = apply_commands(act, [merge_cmd], notifier)
            if results and results[0].get("reason") == "not a command":
                return False
        elif prop_cmd.action in ("status", "more"):
            _page(act, notifier, triage_file, more=prop_cmd.action == "more")
        elif prop_cmd.action == "all":
            _all(act, notifier, triage_file)
        elif prop_cmd.action == "help":
            notifier.send(HELP)
        elif prop_cmd.action == "decisions":
            # read-only: the state file belongs to the daemon's decisions_tick
            first_seen = decisions.read_state(decisions.state_path())["first_seen"]
            for chunk in decisions.format_decisions(decisions.collect(act),
                                                    datetime.now(timezone.utc), first_seen):
                notifier.send(chunk)
        elif prop_cmd.action == "run":
            text = actions.run_action(prop_cmd.targets[0], update_id=update_id)
            for chunk in _chunks(text.split("\n")):
                notifier.send(chunk)
        elif prop_cmd.action == "show":
            text = actions.show_action(prop_cmd.targets[0])
            for chunk in _chunks(text.split("\n")):
                notifier.send(chunk)
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
