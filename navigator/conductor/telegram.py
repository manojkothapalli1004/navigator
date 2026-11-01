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
                 json={"chat_id": chat, "text": card_text(card)[:4000], "reply_markup": buttons(card)},
                 timeout=15)
        return bool(r.json().get("ok"))
    except Exception as e:  # the URL holds the token: log the class name only
        _log.warning("conductor.telegram_send_failed: %s", type(e).__name__)
        return False


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Telegram caps a message at 4096 chars: cut at a newline in the back half, else hard."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut > limit // 2:
            parts.append(text[:cut])
            text = text[cut + 1:]
        else:
            parts.append(text[:limit])
            text = text[limit:]
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
