import os
import re
from pathlib import Path
from typing import Protocol
import httpx


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class NullNotifier:
    def send(self, text: str) -> None:
        pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=10.0)

    def send(self, text: str) -> None:
        # best-effort: an unreachable Telegram must never fail a run or a cycle
        try:
            self.client.post(self.url, json={"chat_id": self.chat_id, "text": text[:4000]})
        except Exception:
            pass


def _read_env_file(path: str) -> dict[str, str]:
    values = {}
    for line in Path(path).expanduser().read_text().splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return values


def load_telegram_credentials() -> tuple[str, str] | None:
    """Direct NAVIGATOR_TELEGRAM_* vars win; otherwise read the bot's own .env
    (NAVIGATOR_NOTIFY_ENV_FILE) so the token isn't duplicated into the plist."""
    token = os.getenv("NAVIGATOR_TELEGRAM_BOT_TOKEN")
    chat = os.getenv("NAVIGATOR_TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    env_file = os.getenv("NAVIGATOR_NOTIFY_ENV_FILE")
    if env_file and Path(env_file).expanduser().is_file():
        values = _read_env_file(env_file)
        token, chat = values.get("TELEGRAM_BOT_TOKEN"), values.get("AUTHORIZED_CHAT_ID")
        if token and chat:
            return token, chat
    return None


def make_notifier() -> Notifier:
    creds = load_telegram_credentials()
    return TelegramNotifier(*creds) if creds else NullNotifier()


def _diff_summary(diff: str) -> str:
    files = re.findall(r"^diff --git a/(\S+)", diff, re.M)
    adds = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return f"{', '.join(files) or 'no files'} (+{adds}/-{dels})"


def format_verification(log: list[dict]) -> str:
    if not log:
        return "verification: (none recorded)"
    lines = ["verification:"]
    for c in log:
        tail = (c.get("output_tail") or "").strip().splitlines()
        lines.append(f"  $ {c.get('argv')}  rc={c.get('returncode')}  {tail[-1] if tail else ''}")
    return "\n".join(lines)


class NotifyingACT:
    """ACT client wrapper that turns run events into operator messages."""

    def __init__(self, inner, notifier: Notifier):
        self.inner = inner
        self.notifier = notifier
        self._blocked_announced: set[str] = set()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def emit_event(self, event) -> None:
        self.inner.emit_event(event)
        text = self._message(event.kind, event.payload)
        if text:
            self.notifier.send(text)

    def _message(self, kind: str, p: dict) -> str | None:
        run_id = p.get("run_id", "?")
        if kind == "run.awaiting_review":
            return (f"Navigator: {run_id} is ready for review\n{p.get('title', '')}\n"
                    f"{_diff_summary(p.get('diff', ''))}\n"
                    f"{format_verification(p.get('verification') or [])}\n"
                    f"review: python -m navigator diff {run_id}\n"
                    f"merge:  python -m navigator merge {run_id}\n"
                    f"reject: python -m navigator reject {run_id}")
        if kind == "run.merge_blocked":
            if run_id in self._blocked_announced:
                return None
            self._blocked_announced.add(run_id)
            return f"Navigator: merge of {run_id} is waiting — {p.get('reason', '')}"
        if kind == "run.failed":
            return f"Navigator: {run_id} FAILED — {p.get('error') or 'see outcome'}"
        if kind == "run.declined":
            return f"Navigator: {run_id} declined; worktree kept for inspection"
        if kind == "run.completed":
            return f"Navigator: {run_id} merged"
        return None
