"""Operator merge decisions typed in Telegram (N5, hardened in N5b).

TelegramInbox reads the bot's updates (getUpdates, timeout=0: no long poll inside
the daemon cycle) for the one authorized operator only. A text message counts
only when it arrives in the operator's private chat: chat.type == 'private' and
chat.id == from.id == the authorized chat id (group members, channels, edited
messages and callback queries are ignored). Those messages are appended to a
JSONL inbox (mode 0600, rotated to .1 above 1 MB) so other tools read operator
messages from the file instead of calling getUpdates themselves. The exact-form
commands '/merge <run-id>' and '/reject <run-id>' (full run id, one line) are
returned, as are the N8 shorthands 'merge' / 'reject' (no argument: the one run
waiting for merge) and 'merge <name>' / 'reject <name>' (<name> = first token of
the proposal title without a trailing ':', e.g. 'N5b'); the leading '/' is optional
and the word is case-insensitive. Commands count only when the message is not
forwarded and is less than an hour old (the clock is injectable for tests). A
shorthand is resolved at apply time against live ACT state with every check the
full-id form applies; zero or several candidates apply nothing and get a reply.

poll_updates(long_poll_s) is the always-on reader's entry point (N11,
telegram_reader.py): same filtering, inbox file and offset as poll_once, but
Telegram holds the request up to long_poll_s and (update_id, raw message) pairs
come back for telegram_proposals.handle_operator_message, so status/approve/
decline are answered at once instead of once per daemon cycle; the update_id is
what a merge decision event records. poll_messages(long_poll_s) is the same poll
without the update ids: the raw authorized text messages only.

Navigator must be the only getUpdates consumer of the bot: Telegram answers a
second consumer with HTTP 409, which poll_once, poll_updates and poll_messages
report as TelegramError('another Telegram getUpdates consumer is active'). Every network
failure becomes a TelegramError with a fixed message: the URL carries the bot
token and must never reach a log, a summary or a notification.

Without a persisted offset (first start, missing or corrupt offset file) the
first poll only learns the current position and processes nothing: Telegram keeps
unconfirmed updates for 24h and replaying old /merge commands is not safe.

apply_commands records each decision in ACT, but only for an open run that is
awaiting review, has a pending merge approval and was marked audited
(`navigator audited <run-id>`). It never merges anything itself: the daemon's
merge_poll acts on the recorded decision in execute mode, exactly as for the CLI
`merge`; outside execute mode the reply says so. A command that fails to apply
(its offset is already persisted) is answered with the exception class name so
the operator knows to re-send it.
"""
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from navigator.cli import is_audited, merge_tier_of
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.event import Event
from navigator.observability.logging import get_logger
from navigator.observability.notify import load_telegram_credentials
from navigator.runtime.merge_tiers import batch_enabled

log = get_logger("navigator.telegram_inbox")

TELEGRAM_ACTOR = "operator-telegram"
RUN_ID = re.compile(r"run-[0-9A-HJKMNP-TV-Z]{26}")
_ACTIONS = {"/merge": "merge", "/reject": "reject"}
# 'merge <name>': a proposal short name such as 'N5b' or 'S3a' (never a run id prefix)
SHORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
DEFAULT_OFFSET_PATH = "/Users/Manoj/calwbot/logs/navigator/telegram_offset"
DEFAULT_INBOX_PATH = "/Users/Manoj/calwbot/logs/navigator/telegram_inbox.jsonl"

# a command older than this is not acted on (the daemon may have been down)
MAX_MESSAGE_AGE_S = 3600
# Telegram's clock vs ours: a message a few seconds "in the future" is still fresh
MAX_CLOCK_SKEW_S = 60
INBOX_MAX_BYTES = 1024 * 1024
INBOX_MODE = 0o600

CONFLICT_MESSAGE = "another Telegram getUpdates consumer is active"
FETCH_FAILED_MESSAGE = "telegram getUpdates failed"


class TelegramError(RuntimeError):
    """getUpdates failed. The message is fixed text: never the URL (it carries the
    bot token) and never another exception's text."""


@dataclass(frozen=True)
class TelegramCommand:
    action: str  # "merge" | "reject"
    run_id: str | None  # full run id; None for the shorthand forms (resolved at apply time)
    update_id: int
    name: str | None = None  # 'merge <name>': proposal short name; None for bare 'merge'
    batch: bool = False  # 'merge all [except ...]' (3c)
    exclude: tuple[str, ...] = ()  # names/run ids left out of 'merge all'


def short_name(title: str) -> str:
    """First token of a proposal title with a trailing ':' removed: 'N5b: ...' -> 'N5b'."""
    parts = title.split()
    return parts[0].rstrip(":") if parts else ""


def parse_command(text, update_id: int) -> TelegramCommand | None:
    """One line, whole message, whitespace trimmed; the command word is case-insensitive
    and its leading '/' is optional:
      '/merge <run-id>' / '/reject <run-id>'  -> full run id (must match RUN_ID)
      'merge' / 'reject'                      -> the one run waiting for merge
      'merge <name>' / 'reject <name>'        -> the run whose proposal short name is <name>
      'merge all' / 'merge all except a,b'    -> every audited tier A/B run at the gate
                                                 (3c; excluded names are short names or
                                                 FULL run ids, comma or space separated)
    Anything else is not a command ('merge looks good?' is ordinary chat). Consequence:
    a proposal short-named 'all' can no longer be merged by name; use its run id."""
    if not isinstance(text, str) or "\n" in text or "\r" in text:
        return None
    parts = text.strip().split()
    if not parts:
        return None
    word = parts[0].lower()
    action = _ACTIONS.get(word if word.startswith("/") else "/" + word)
    if action is None:
        return None
    if len(parts) >= 2 and parts[1].lower() == "all":
        if action != "merge":
            return None
        if len(parts) == 2:
            return TelegramCommand(action="merge", run_id=None, update_id=update_id, batch=True)
        if parts[2].lower() != "except" or len(parts) < 4:
            return None
        # commas only inside the except list
        names = tuple(n for chunk in parts[3:] for n in chunk.split(",") if n)
        # each excluded name is a short name OR a FULL run id (SHORT_NAME alone would
        # accept a truncated "run-01ABC"; the single-run path refuses those too). A
        # candidate whose proposal vanished has its run id as its short name and must
        # still be excludable.
        if not names or any((n.lower().startswith("run-") and not RUN_ID.fullmatch(n))
                            or not (RUN_ID.fullmatch(n) or SHORT_NAME.fullmatch(n))
                            for n in names):
            return None
        return TelegramCommand(action="merge", run_id=None, update_id=update_id, batch=True,
                               exclude=names)
    if len(parts) > 2:
        return None
    if len(parts) == 1:
        return TelegramCommand(action=action, run_id=None, update_id=update_id)
    arg = parts[1]
    if RUN_ID.fullmatch(arg):
        return TelegramCommand(action=action, run_id=arg, update_id=update_id)
    # a truncated run id is not a short name: never guess which run was meant
    if arg.lower().startswith("run-") or not SHORT_NAME.fullmatch(arg):
        return None
    return TelegramCommand(action=action, run_id=None, update_id=update_id, name=arg)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_operator_message(msg: dict, chat_id: str) -> bool:
    """Sent by the operator, in the operator's private chat."""
    chat = msg.get("chat")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or str(chat.get("id")) != str(chat_id)):
        return False
    sender = msg.get("from")
    return isinstance(sender, dict) and str(sender.get("id")) == str(chat_id)


class TelegramInbox:
    def __init__(self, token: str, chat_id: str, offset_path, inbox_path,
                 client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.url = f"https://api.telegram.org/bot{token}/getUpdates"
        self.chat_id = str(chat_id)
        self.offset_path = Path(offset_path)
        self.inbox_path = Path(inbox_path)
        self.client = client or httpx.Client(timeout=10.0)
        self.clock = clock

    # ---- persistence -----------------------------------------------------------

    def _load_offset(self) -> int | None:
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _save_offset(self, offset: int) -> None:
        # atomic: write a temp file in the same directory, then rename over
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.offset_path.parent, prefix=".telegram_offset.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(offset))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.offset_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _rotate_inbox(self) -> None:
        try:
            size = self.inbox_path.stat().st_size
        except FileNotFoundError:
            return
        if size > INBOX_MAX_BYTES:
            os.replace(self.inbox_path, self.inbox_path.with_name(self.inbox_path.name + ".1"))

    def _append_inbox(self, entries: list[dict]) -> None:
        if not entries:
            return
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_inbox()
        # operator messages are private: owner read/write only, even if the file
        # was created world-readable by an older version
        fd = os.open(self.inbox_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, INBOX_MODE)
        try:
            os.fchmod(fd, INBOX_MODE)
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")

    # ---- filtering -------------------------------------------------------------

    def _authorized(self, msg: dict) -> bool:
        return is_operator_message(msg, self.chat_id)

    @staticmethod
    def _ignored_reason(msg: dict, now: float) -> str | None:
        """Why an authorized message is still not a command (None = acceptable)."""
        if any(isinstance(k, str) and k.startswith("forward_") for k in msg):
            return "forwarded"
        date = msg.get("date")
        if not _is_int(date):
            return "no_date"
        if date < now - MAX_MESSAGE_AGE_S or date > now + MAX_CLOCK_SKEW_S:
            return "stale"
        return None

    # ---- polling ---------------------------------------------------------------

    def _fetch(self, params: dict, timeout_s: float | None = None) -> list[dict]:
        # every failure becomes fixed text: the URL contains the bot token
        try:
            if timeout_s is None:
                r = self.client.get(self.url, params=params)
            else:
                r = self.client.get(self.url, params=params, timeout=timeout_s)
        except Exception:
            raise TelegramError(FETCH_FAILED_MESSAGE) from None
        if r.status_code == 409:
            raise TelegramError(CONFLICT_MESSAGE)
        if r.status_code != 200:
            raise TelegramError(f"telegram getUpdates HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            raise TelegramError("telegram getUpdates returned invalid JSON") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError("telegram getUpdates returned ok=false")
        result = data.get("result") or []
        if not isinstance(result, list):
            raise TelegramError("telegram getUpdates returned no result list")
        return result

    def _consume(self, long_poll_s: int) -> list[tuple[dict, dict]]:
        """Fetch new updates, append the operator's text messages to the inbox file and
        persist the offset. Returns one (inbox entry, raw message) pair per authorized
        text message, ignored ones included (entry['ignored'] says why). long_poll_s > 0
        asks Telegram to hold the request (the always-on reader); 0 returns at once
        (the daemon cycle)."""
        offset = self._load_offset()
        params = {"timeout": long_poll_s}
        if offset is not None:
            params["offset"] = offset
        # a long poll must outlive Telegram's hold time; 0 keeps the client's own timeout
        result = self._fetch(params, timeout_s=long_poll_s + 10 if long_poll_s > 0 else None)
        updates = [(u["update_id"], u) for u in result
                   if isinstance(u, dict) and _is_int(u.get("update_id"))]
        if offset is None:
            # no persisted position: learn it and process nothing, otherwise up to
            # 24h of old /merge commands would be replayed
            last = max(uid for uid, _ in updates) if updates else None
            self._save_offset(last + 1 if last is not None else 0)
            return []
        now = self.clock()
        entries: list[dict] = []
        pairs: list[tuple[dict, dict]] = []
        last = None
        for uid, update in updates:
            if uid < offset:
                continue  # already consumed: never processed twice
            last = uid if last is None else max(last, uid)
            msg = update.get("message")  # edited_message/channel_post/callback_query: ignored
            if not isinstance(msg, dict) or not self._authorized(msg):
                continue
            text = msg.get("text")
            if not isinstance(text, str):
                continue
            entry = {"update_id": uid, "date": msg.get("date"), "text": text}
            ignored = self._ignored_reason(msg, now)
            if ignored is not None:
                entry["ignored"] = ignored
            entries.append(entry)
            pairs.append((entry, msg))
        self._append_inbox(entries)
        if last is not None:
            self._save_offset(last + 1)
        return pairs

    def poll_once(self) -> list[TelegramCommand]:
        """One short poll (timeout=0) for the daemon cycle: the merge commands found."""
        commands: list[TelegramCommand] = []
        for entry, _msg in self._consume(0):
            if "ignored" in entry:
                continue
            cmd = parse_command(entry["text"], entry["update_id"])
            if cmd is not None:
                commands.append(cmd)
        return commands

    def poll_updates(self, long_poll_s: int = 25) -> list[tuple[int, dict]]:
        """One long poll for the always-on reader (N11): (update_id, raw message) pairs
        for the authorized text messages, ignored (forwarded/stale) ones included -
        handle_operator_message applies its own freshness checks. The update_id is
        what a merge decision event records. Inbox file and offset are updated as
        for poll_once."""
        return [(entry["update_id"], msg) for entry, msg in self._consume(long_poll_s)]

    def poll_messages(self, long_poll_s: int = 25) -> list[dict]:
        """poll_updates without the update ids: the raw authorized text messages."""
        return [msg for _uid, msg in self.poll_updates(long_poll_s)]


def _apply_wording() -> str:
    """What happens next, honestly: merge_poll merges in execute mode, or in dry_run
    when the operator opted in with NAVIGATOR_MERGE_IN_DRY_RUN (N8)."""
    mode = os.getenv("NAVIGATOR_MODE", "dry_run")
    if mode == "execute":
        return "applies this cycle"
    flag = (os.getenv("NAVIGATOR_MERGE_IN_DRY_RUN") or "").strip().lower()
    if mode == "dry_run" and flag in ("1", "true"):
        return "applies this cycle"
    return "recorded; merges apply only in execute mode"


def _label(cmd: TelegramCommand) -> str:
    """How the operator typed it, for replies: '/merge <run-id>', '/merge N5b', '/merge',
    '/merge all' or '/merge all except a,b'."""
    if cmd.batch:
        return "/merge all" + (f" except {','.join(cmd.exclude)}" if cmd.exclude else "")
    arg = cmd.run_id or cmd.name
    return f"/{cmd.action} {arg}" if arg else f"/{cmd.action}"


def _candidates(act) -> list[tuple]:
    """Runs a shorthand command may mean: open, awaiting review, with a pending merge
    approval and audited - the checks the full-id form applies, evaluated right now.
    Each entry is (run, short name, title). A proposal that cannot be fetched (vanished,
    ACT hiccup) does not break the listing: the run is named by its id instead."""
    out = []
    for run in act.list_open_runs():
        if run.phase != RunPhase.awaiting_review:
            continue
        if not act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                  decision=ApprovalDecision.pending):
            continue
        if not is_audited(act, run.id):
            continue
        try:
            prop = act.get_proposal(run.proposal_id)
        except Exception as e:
            log.warning("telegram.candidate_proposal_failed", run_id=run.id,
                        error=type(e).__name__)
            prop = None
        title = (prop.title if prop is not None and prop.title else "").strip()
        out.append((run, short_name(title) or run.id, title or run.id))
    return out


def _reply_arg(run, short: str) -> str:
    """What to type after 'merge': the short name when parse_command accepts it, else
    the full run id (non-ASCII names, names that look like a run id prefix)."""
    if short.lower().startswith("run-") or not SHORT_NAME.fullmatch(short):
        return run.id
    return short


def _candidate_lines(cands, action: str) -> str:
    return "\n".join(f"{short} - {title} - reply: {action} {_reply_arg(run, short)}"
                     for run, short, title in cands)


def _resolve(act, cmd: TelegramCommand, notifier) -> tuple[tuple | None, dict | None]:
    """Shorthand -> candidate against live ACT state. Exactly one match:
    ((run, short, title), None). Otherwise nothing is touched, the operator gets a
    reply and (None, result) returns."""
    cands = _candidates(act)
    if cmd.name is None:
        matches = cands
    else:
        matches = [c for c in cands if c[1].lower() == cmd.name.lower()]
    if len(matches) == 1:
        return matches[0], None
    if not cands:
        reason = "nothing waiting for merge"
        text = f"Navigator: {_label(cmd)} - nothing waiting for merge"
    else:
        reason = ("ambiguous: several runs waiting for merge" if cmd.name is None
                  else f"no single run named {cmd.name!r}")
        text = (f"Navigator: {_label(cmd)} not applied - {reason}; "
                f"{len(cands)} waiting for merge:\n{_candidate_lines(cands, cmd.action)}")
    notifier.send(text)
    return None, {"run_id": None, "action": cmd.action, "applied": False, "reason": reason}


def _reply(notifier, text: str) -> None:
    try:
        notifier.send(text)
    except Exception as e:
        log.warning("telegram.reply_failed", error=type(e).__name__)


def _apply_batch(act, cmd: TelegramCommand, notifier) -> dict:
    """'merge all [except names]': approve the pending merge approval of every candidate
    (audited, pending, awaiting review) whose recorded tier is A or B. Tier C and
    unknown tiers are listed and left alone (they need 'merge <name>'); excluded names
    are listed. One reply, one run.telegram_decision event per approved run. Only a
    raise BEFORE the loop propagates to apply_commands; per-run failures are reported
    in the summary so a re-sent 'merge all' cannot lose the runs already approved."""
    if not batch_enabled(act):
        notifier.send("Navigator: merge all is off - set NAVIGATOR_MERGE_TIER_B_BATCH=1 "
                      "for the Telegram process")
        return {"run_id": None, "action": "merge", "applied": False, "reason": "batch off"}
    cands = _candidates(act)
    if not cands:
        notifier.send("Navigator: merge all - nothing waiting for merge")
        return {"run_id": None, "action": "merge", "applied": False,
                "reason": "nothing waiting for merge"}
    excluded = {e.lower() for e in cmd.exclude}
    known = ({short.lower() for _, short, _ in cands}
             | {run.id.lower() for run, _, _ in cands})
    # operator's own spelling in the reply
    unmatched = sorted(e for e in cmd.exclude if e.lower() not in known)
    if unmatched:
        # a typo in the except list must never merge the run the operator meant to hold back
        notifier.send("Navigator: merge all NOT applied - unknown name(s) in except: "
                      + ", ".join(unmatched) + "\nwaiting: "
                      + ", ".join(_reply_arg(run, short) for run, short, _ in cands))
        return {"run_id": None, "action": "merge", "applied": False,
                "reason": "unknown excluded name"}
    approved, tier_c, unknown, left_out, failed, already = [], [], [], [], [], []
    event_failed = False
    for run, short, title in cands:
        if short.lower() in excluded or run.id.lower() in excluded:
            left_out.append(short)
            continue
        tier = merge_tier_of(act, run)
        if tier is None:
            unknown.append(f"merge {_reply_arg(run, short)}")
            continue
        if tier == "C":
            tier_c.append(f"merge {_reply_arg(run, short)}")
            continue
        try:
            pending = act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                         decision=ApprovalDecision.pending)
            if not pending:
                already.append(short)  # decided between _candidates and this re-read
                continue
            act.decide_approval(pending[0].id, ApprovalDecision.approved, actor=TELEGRAM_ACTOR)
        except Exception as e:
            # one run's ACT failure must not hide the runs already approved
            log.warning("telegram.batch_run_failed", run_id=run.id, error=type(e).__name__)
            failed.append(f"{short} ({type(e).__name__})")
            continue
        try:
            act.emit_event(Event(kind="run.telegram_decision", correlation_id=run.correlation_id,
                                 payload={"run_id": run.id, "decision": "approved",
                                          "update_id": cmd.update_id, "batch": True}))
        except Exception as e:
            log.warning("telegram.decision_event_failed", run_id=run.id,
                        error=type(e).__name__)
            event_failed = True
        approved.append((run.id, f"{short} - {title} (tier {tier})"))
    # fixed wording (not _apply_wording()): after the first batch merge lands, merge_poll
    # rebases the siblings and a sibling whose diff changed goes stale and needs a fresh
    # audit + word
    lines = [f"Navigator: merge all - {len(approved)} merge(s) approved from Telegram; "
             "merges apply as each sibling rebase stays clean"
             + (" (event log failed)" if event_failed else "")]
    lines += [f"- {text}" for _, text in approved]
    if tier_c:
        lines.append("tier C, needs its own word: " + ", ".join(tier_c))
    if unknown:
        lines.append("tier unknown, needs its own word: " + ", ".join(unknown))
    if left_out:
        lines.append("excluded: " + ", ".join(left_out))
    if failed:
        lines.append("failed, re-send 'merge <name>' for: " + ", ".join(failed))
    if already:
        lines.append("already decided meanwhile: " + ", ".join(already))
    try:
        notifier.send("\n".join(lines))
    except Exception as e:
        # approvals stand; the log is the record
        log.warning("telegram.batch_reply_failed", error=type(e).__name__)
    return {"run_id": None, "action": "merge", "applied": bool(approved),
            "reason": None if approved else "nothing applicable",
            "approved": [rid for rid, _ in approved]}


def _apply_one(act, cmd: TelegramCommand, notifier) -> dict:
    if cmd.batch:
        return _apply_batch(act, cmd, notifier)
    run_id = cmd.run_id
    resolved = None
    if run_id is None:
        # shorthand: resolve to a run first; zero/ambiguous candidates reply and stop
        resolved, failed = _resolve(act, cmd, notifier)
        if failed is not None:
            return failed
        run_id = resolved[0].id
    run = next((r for r in act.list_open_runs() if r.id == run_id), None)
    pending = []
    if run is None:
        reason = "unknown or closed run"
    elif run.phase != RunPhase.awaiting_review:
        reason = "not awaiting review"
    else:
        pending = act.list_approvals(target=run_id, gate=ApprovalGate.merge,
                                     decision=ApprovalDecision.pending)
        if not pending:
            reason = "no pending merge approval"
        elif not is_audited(act, run_id):
            reason = "not audited yet"
        else:
            reason = None
    if reason is not None:
        notifier.send(f"Navigator: /{cmd.action} {run_id} not applied - {reason}")
        return {"run_id": run_id, "action": cmd.action, "applied": False, "reason": reason}
    merge = cmd.action == "merge"
    decision = ApprovalDecision.approved if merge else ApprovalDecision.declined
    verb = "approved" if merge else "rejected"
    act.decide_approval(pending[0].id, decision, actor=TELEGRAM_ACTOR)
    # the decision is recorded from here on: a failing event log must not make the
    # reply claim otherwise
    event_note = ""
    try:
        act.emit_event(Event(kind="run.telegram_decision", correlation_id=run.correlation_id,
                             payload={"run_id": run_id, "decision": verb,
                                      "update_id": cmd.update_id}))
    except Exception as e:
        log.warning("telegram.decision_event_failed", run_id=run_id,
                    error=type(e).__name__)
        event_note = " (event log failed)"
    # a shorthand resolved against live state: say which run it picked, by name
    who = run_id if resolved is None else f"{resolved[1]} - {resolved[2]} ({run_id})"
    notifier.send(f"Navigator: {who} merge {verb} from Telegram "
                  f"({_apply_wording()}){event_note}")
    return {"run_id": run_id, "action": cmd.action, "applied": True, "reason": None}


def apply_commands(act, commands: list[TelegramCommand], notifier) -> list[dict]:
    """Record each command's decision in ACT when every check passes; otherwise change
    nothing and reply with the reason. A command whose application raises (ACT down,
    ...) is already consumed (its offset is persisted): the operator is told to
    re-send it, with the exception class name only."""
    results = []
    for cmd in commands:
        try:
            results.append(_apply_one(act, cmd, notifier))
        except Exception as e:
            name = type(e).__name__
            log.warning("telegram.apply_failed", command=_label(cmd), error=name)
            _reply(notifier, f"Navigator: could not apply {_label(cmd)}: "
                             f"{name} - re-send it")
            results.append({"run_id": cmd.run_id, "action": cmd.action, "applied": False,
                            "reason": f"error: {name}"})
    return results


def make_telegram_inbox() -> TelegramInbox | None:
    """Opt-in: only when NAVIGATOR_TELEGRAM_COMMANDS=1 and credentials exist."""
    if os.getenv("NAVIGATOR_TELEGRAM_COMMANDS") != "1":
        return None
    creds = load_telegram_credentials()
    if not creds:
        return None
    token, chat = creds
    return TelegramInbox(token, chat,
                         os.getenv("NAVIGATOR_TELEGRAM_OFFSET_PATH") or DEFAULT_OFFSET_PATH,
                         os.getenv("NAVIGATOR_TELEGRAM_INBOX_PATH") or DEFAULT_INBOX_PATH)
