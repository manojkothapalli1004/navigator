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
            f"Reply  ok {card['code']}  to run it, or  no {card['code']}. Expires in 60 min.")


def send_card(card: dict, creds: tuple[str, str] | None, post=httpx.post) -> bool:
    if not creds:
        return False
    tok, chat = creds
    try:
        r = post(f"https://api.telegram.org/bot{tok}/sendMessage",
                 json={"chat_id": chat, "text": card_text(card)[:4000]}, timeout=15)
        return bool(r.json().get("ok"))
    except Exception as e:  # the URL holds the token: log the class name only
        _log.warning("conductor.telegram_send_failed: %s", type(e).__name__)
        return False
