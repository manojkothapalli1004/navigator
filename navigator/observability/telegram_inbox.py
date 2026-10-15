"""Operator merge decisions typed in Telegram (N5).

TelegramInbox reads the bot's updates (getUpdates, timeout=0: no long poll inside
the daemon cycle) for the one authorized chat only. It appends every authorized
text message to a JSONL inbox and returns the exact-form commands
'/merge <run-id>' and '/reject <run-id>' (full run id only).

apply_commands records each decision in ACT, but only for an open run that is
awaiting review, has a pending merge approval and was marked audited
(`navigator audited <run-id>`). It never merges anything itself: the daemon's
merge_poll acts on the recorded decision, exactly as for the CLI `merge`.
"""
import json
import os
import re
import tempfile
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


@dataclass(frozen=True)
class TelegramCommand:
    action: str  # "merge" | "reject"
    run_id: str
    update_id: int


def parse_command(text, update_id: int) -> TelegramCommand | None:
    """'/merge <run-id>' or '/reject <run-id>': the command word is case-insensitive,
    the run id must be the full id. Anything else is not a command."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if len(parts) != 2:
        return None
    action = _ACTIONS.get(parts[0].lower())
    if action is None or not RUN_ID.fullmatch(parts[1]):
        return None
    return TelegramCommand(action=action, run_id=parts[1], update_id=update_id)


class TelegramInbox:
    def __init__(self, token: str, chat_id: str, offset_path, inbox_path,
                 client: httpx.Client | None = None):
        self.url = f"https://api.telegram.org/bot{token}/getUpdates"
        self.chat_id = str(chat_id)
        self.offset_path = Path(offset_path)
        self.inbox_path = Path(inbox_path)
        self.client = client or httpx.Client(timeout=10.0)

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

    def _append_inbox(self, entries: list[dict]) -> None:
        if not entries:
            return
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")

    def poll_once(self) -> list[TelegramCommand]:
        offset = self._load_offset()
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        # errors are re-raised without the URL: it contains the bot token
        try:
            r = self.client.get(self.url, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError(f"telegram getUpdates failed: {type(e).__name__}") from None
        if r.status_code != 200:
            raise RuntimeError(f"telegram getUpdates HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("telegram getUpdates returned ok=false")
        entries: list[dict] = []
        commands: list[TelegramCommand] = []
        last = None
        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            uid = update.get("update_id")
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            if offset is not None and uid < offset:
                continue  # already consumed: never processed twice
            last = uid if last is None else max(last, uid)
            msg = update.get("message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat")
            if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
                continue  # only the authorized chat
            text = msg.get("text")
            if not isinstance(text, str):
                continue
            entries.append({"update_id": uid, "date": msg.get("date"), "text": text})
            cmd = parse_command(text, uid)
            if cmd is not None:
                commands.append(cmd)
        self._append_inbox(entries)
        if last is not None:
            self._save_offset(last + 1)
        return commands


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
    act.emit_event(Event(kind="run.telegram_decision", correlation_id=run.correlation_id,
                         payload={"run_id": cmd.run_id, "decision": verb,
                                  "update_id": cmd.update_id}))
    notifier.send(f"Navigator: {cmd.run_id} merge {verb} from Telegram (applies this cycle)")
    return {"run_id": cmd.run_id, "action": cmd.action, "applied": True, "reason": None}


def apply_commands(act, commands: list[TelegramCommand], notifier) -> list[dict]:
    """Record each command's decision in ACT when every check passes; otherwise change
    nothing and reply with the reason."""
    results = []
    for cmd in commands:
        try:
            results.append(_apply_one(act, cmd, notifier))
        except Exception as e:
            log.warning("telegram.apply_failed", run_id=cmd.run_id, error=type(e).__name__)
            results.append({"run_id": cmd.run_id, "action": cmd.action, "applied": False,
                            "reason": f"error: {type(e).__name__}"})
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
