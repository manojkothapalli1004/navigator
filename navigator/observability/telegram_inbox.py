"""Operator merge decisions typed in Telegram (N5, hardened in N5b).

TelegramInbox reads the bot's updates (getUpdates, timeout=0: no long poll inside
the daemon cycle) for the one authorized operator only. A text message counts
only when it arrives in the operator's private chat: chat.type == 'private' and
chat.id == from.id == the authorized chat id (group members, channels, edited
messages and callback queries are ignored). Those messages are appended to a
JSONL inbox (mode 0600, rotated to .1 above 1 MB) so other tools read operator
messages from the file instead of calling getUpdates themselves. The exact-form
commands '/merge <run-id>' and '/reject <run-id>' (full run id, one line) are
returned, but only when the message is not forwarded and is less than an hour old
(the clock is injectable for tests).

Navigator must be the only getUpdates consumer of the bot: Telegram answers a
second consumer with HTTP 409, which poll_once reports as
TelegramError('another Telegram getUpdates consumer is active'). Every network
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

from navigator.cli import is_audited
from navigator.contracts.enums import ApprovalDecision, ApprovalGate, RunPhase
from navigator.contracts.event import Event
from navigator.observability.logging import get_logger
from navigator.observability.notify import load_telegram_credentials

log = get_logger("navigator.telegram_inbox")

TELEGRAM_ACTOR = "operator-telegram"
RUN_ID = re.compile(r"run-[0-9A-HJKMNP-TV-Z]{26}")
_ACTIONS = {"/merge": "merge", "/reject": "reject"}
DEFAULT_OFFSET_PATH = "/Users/pradeep/calwbot/logs/navigator/telegram_offset"
DEFAULT_INBOX_PATH = "/Users/pradeep/calwbot/logs/navigator/telegram_inbox.jsonl"

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
    run_id: str
    update_id: int


def parse_command(text, update_id: int) -> TelegramCommand | None:
    """'/merge <run-id>' or '/reject <run-id>' on a single line: the command word is
    case-insensitive, the run id must be the full id. Anything else is not a command."""
    if not isinstance(text, str) or "\n" in text or "\r" in text:
        return None
    parts = text.strip().split()
    if len(parts) != 2:
        return None
    action = _ACTIONS.get(parts[0].lower())
    if action is None or not RUN_ID.fullmatch(parts[1]):
        return None
    return TelegramCommand(action=action, run_id=parts[1], update_id=update_id)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
        """Sent by the operator, in the operator's private chat."""
        chat = msg.get("chat")
        if (not isinstance(chat, dict) or chat.get("type") != "private"
                or str(chat.get("id")) != self.chat_id):
            return False
        sender = msg.get("from")
        return isinstance(sender, dict) and str(sender.get("id")) == self.chat_id

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

    def _fetch(self, params: dict) -> list[dict]:
        # every failure becomes fixed text: the URL contains the bot token
        try:
            r = self.client.get(self.url, params=params)
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

    def poll_once(self) -> list[TelegramCommand]:
        offset = self._load_offset()
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        result = self._fetch(params)
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
        commands: list[TelegramCommand] = []
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
            if ignored is not None:
                continue
            cmd = parse_command(text, uid)
            if cmd is not None:
                commands.append(cmd)
        self._append_inbox(entries)
        if last is not None:
            self._save_offset(last + 1)
        return commands


def _apply_wording() -> str:
    """What happens next, honestly: merge_poll merges only in execute mode."""
    if os.getenv("NAVIGATOR_MODE", "dry_run") == "execute":
        return "applies this cycle"
    return "recorded; merges apply only in execute mode"


def _reply(notifier, text: str) -> None:
    try:
        notifier.send(text)
    except Exception as e:
        log.warning("telegram.reply_failed", error=type(e).__name__)


def _apply_one(act, cmd: TelegramCommand, notifier) -> dict:
    run = next((r for r in act.list_open_runs() if r.id == cmd.run_id), None)
    pending = []
    if run is None:
        reason = "unknown or closed run"
    elif run.phase != RunPhase.awaiting_review:
        reason = "not awaiting review"
    else:
        pending = act.list_approvals(target=cmd.run_id, gate=ApprovalGate.merge,
                                     decision=ApprovalDecision.pending)
        if not pending:
            reason = "no pending merge approval"
        elif not is_audited(act, cmd.run_id):
            reason = "not audited yet"
        else:
            reason = None
    if reason is not None:
        notifier.send(f"Navigator: /{cmd.action} {cmd.run_id} not applied - {reason}")
        return {"run_id": cmd.run_id, "action": cmd.action, "applied": False, "reason": reason}
    merge = cmd.action == "merge"
    decision = ApprovalDecision.approved if merge else ApprovalDecision.declined
    verb = "approved" if merge else "rejected"
    act.decide_approval(pending[0].id, decision, actor=TELEGRAM_ACTOR)
    # the decision is recorded from here on: a failing event log must not make the
    # reply claim otherwise
    event_note = ""
    try:
        act.emit_event(Event(kind="run.telegram_decision", correlation_id=run.correlation_id,
                             payload={"run_id": cmd.run_id, "decision": verb,
                                      "update_id": cmd.update_id}))
    except Exception as e:
        log.warning("telegram.decision_event_failed", run_id=cmd.run_id,
                    error=type(e).__name__)
        event_note = " (event log failed)"
    notifier.send(f"Navigator: {cmd.run_id} merge {verb} from Telegram "
                  f"({_apply_wording()}){event_note}")
    return {"run_id": cmd.run_id, "action": cmd.action, "applied": True, "reason": None}


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
            log.warning("telegram.apply_failed", run_id=cmd.run_id, error=name)
            _reply(notifier, f"Navigator: could not apply /{cmd.action} {cmd.run_id}: "
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
