"""C3: tell the operator's phone about a new approval card (the bot handles 'ok CODE' replies)."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

ENV = Path("/Users/Manoj/calwbot/manojtest/.env")
_log = logging.getLogger(__name__)


def load_creds(env_path: Path = ENV) -> tuple[str, str] | None:
    try:
        lines = Path(env_path).read_text().splitlines()
    except OSError:
        return None
    env = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("AUTHORIZED_CHAT_ID")
    return (tok, chat) if tok and chat else None


def card_text(card: dict) -> str:
    return (f"Approval needed\n{card['summary']}\nwhy: {card['reason']}\n"
            f"Tap Approve or Reject (or reply  ok {card['code']}  /  no {card['code']}). Expires in 60 min.")


def buttons(card: dict) -> dict:
    # a tap arrives as a callback_query; the Telegram reader turns it into "ok CODE" / "no CODE"
    return {"inline_keyboard": [[{"text": "Approve", "callback_data": f"card:approve:{card['code']}"},
                                 {"text": "Reject", "callback_data": f"card:decline:{card['code']}"}]]}


def send_card(card: dict, creds: tuple[str, str] | None, post=httpx.post) -> bool:
    if not creds:
        return False
    tok, chat = creds
    try:
        r = post(f"https://api.telegram.org/bot{tok}/sendMessage",
                 json={"chat_id": chat, "text": split_text(card_text(card))[0], "reply_markup": buttons(card)},
                 timeout=15)
        return bool(r.json().get("ok"))
    except Exception as e:  # the URL holds the token: log the class name only
        _log.warning("conductor.telegram_send_failed: %s", type(e).__name__)
        return False


def _units(s: str) -> int:
    # Telegram's 4096 limit counts UTF-16 code units: an emoji is 2
    return len(s.encode("utf-16-le", "surrogatepass")) // 2  # a stray surrogate must not raise


def _fit(text: str, limit: int) -> int:
    """Largest prefix length (in characters) whose UTF-16 size is <= limit."""
    n = 0
    for i, ch in enumerate(text):
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > limit:
            return i
    return len(text)


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Telegram caps a message at 4096 UTF-16 units: cut at a newline in the back half, else hard."""
    parts: list[str] = []
    while _units(text) > limit:
        end = max(_fit(text, limit), 1)  # always make progress, even with a tiny limit
        cut = text.rfind("\n", 0, end + 1)
        if cut > end // 2:
            parts.append(text[:cut])
            text = text[cut + 1:]
        else:
            parts.append(text[:end])
            text = text[end:]
    if text or not parts:
        parts.append(text)
    return parts


def send_text(text: str, creds: tuple[str, str] | None, post=httpx.post) -> bool:
    """C4: a reply for a Telegram-originated turn. True only if every chunk was accepted."""
    if not creds:
        return False
    tok, chat = creds
    ok = True
    for chunk in split_text(text or "(no reply text)"):
        try:
            r = post(f"https://api.telegram.org/bot{tok}/sendMessage",
                     json={"chat_id": chat, "text": chunk}, timeout=15)
            ok = bool(r.json().get("ok")) and ok
        except Exception as e:  # the URL holds the token: log the class name only
            _log.warning("conductor.telegram_send_failed: %s", type(e).__name__)
            return False
    return ok


class Sender:
    """One background thread sends every conductor message to Telegram in the order it was
    queued (cards and replies), so a slow send can't let a later message overtake it."""

    def __init__(self, creds: tuple[str, str] | None, post=httpx.post):
        import queue
        import threading
        self.creds, self.post = creds, post
        self._q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-sender")
        self._thread.start()

    def card(self, card: dict) -> None:
        self._q.put(("card", card))

    def text(self, text: str) -> None:
        self._q.put(("text", text))

    def _run(self) -> None:
        while True:
            kind, item = self._q.get()
            try:
                if kind == "card":
                    send_card(item, self.creds, post=self.post)
                else:
                    send_text(item, self.creds, post=self.post)
            except Exception as e:  # never let one message kill the sender
                _log.warning("conductor.telegram_sender_error: %s", type(e).__name__)
