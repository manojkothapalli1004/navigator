"""Always-on Telegram reader (N11).

Operator 2026-10-18: Navigator should read Telegram like chat, with no delay, and
every message must reach the inbox file the Claude session reads. The daemon's
_telegram_step polls getUpdates once per cycle (up to 900 s apart) and only handles
/merge and /reject; this process long-polls getUpdates continuously
(TelegramInbox.poll_messages) and hands every operator message to
telegram_proposals.handle_operator_message in the same iteration, so status, more,
all, approve, decline, help and the merge shorthands are answered at once.

The reader never merges anything and never calls an LLM: merge decisions are
recorded in ACT exactly as from the daemon and merge_poll acts on them.

Telegram allows one getUpdates consumer per bot, so main_reader refuses to start
when NAVIGATOR_TELEGRAM_COMMANDS=1 (the daemon is that consumer). A failing poll
(409 from a second consumer, network down) is reported to the operator at most once
per notice interval PER ERROR KIND (N11c: each kind keeps its own clock and a
successful poll never re-arms a notice, so neither a competing consumer that lets the
reader win a poll now and then nor two alternating error kinds can flood the chat);
the notice states the configured interval. A message whose handling raises is logged
by exception class name only (the getUpdates URL carries the bot token) and the loop
goes on.

Run with: python -m navigator telegram-reader
"""
import os
import signal
import sys
import time

from navigator.clients.act_client import HTTPACTClient
from navigator.observability.logging import get_logger
from navigator.observability.notify import load_telegram_credentials, make_notifier
from navigator.observability.telegram_inbox import (DEFAULT_INBOX_PATH, DEFAULT_OFFSET_PATH,
                                                    TelegramError, TelegramInbox)
from navigator.observability.telegram_proposals import handle_operator_message

log = get_logger("navigator.telegram_reader")

DEFAULT_ACT_BASE_URL = "http://127.0.0.1:8090"
LONG_POLL_S = 25
ERROR_BACKOFF_S = 30.0
NOTICE_INTERVAL_S = 3600.0  # same error kind: at most one notice per interval


class Terminated(BaseException):
    """SIGTERM (launchd stop): leave the loop like a KeyboardInterrupt, exit 0."""


def _on_sigterm(signum, frame):
    raise Terminated()


def _error_kind(e: Exception) -> str:
    """What the operator is told. A TelegramError's message is fixed text; any other
    exception is named only, since its text could carry the token-bearing URL."""
    if isinstance(e, TelegramError):
        return f"{type(e).__name__}: {e}"
    return type(e).__name__


def _interval_text(seconds: float) -> str:
    """The notice interval as the operator reads it: '10 s', '10 min', '1 h', '2 h'."""
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:g} h"


def _notify(notifier, text: str) -> None:
    try:
        notifier.send(text)
    except Exception as e:
        log.warning("telegram_reader.notify_failed", error=type(e).__name__)


def run_reader(inbox, act, notifier, chat_id, *, long_poll_s: int = LONG_POLL_S,
               error_backoff_s: float = ERROR_BACKOFF_S, max_iterations: int | None = None,
               sleep=time.sleep, clock=time.monotonic,
               notice_interval_s: float = NOTICE_INTERVAL_S) -> int:
    """Long-poll the operator's chat and handle every message as it arrives.
    max_iterations bounds the loop for tests (None = run until interrupted).

    Poll-failure notices (N11c): each error kind is announced at most once per
    notice_interval_s, measured from its own last notice; a successful poll changes
    nothing (no re-arm), so neither a competing getUpdates consumer that lets the
    reader win a poll now and then nor two alternating error kinds can flood the
    chat."""
    chat_id = str(chat_id)
    announced: dict[str, float] = {}  # error kind -> clock() at its last notice
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            messages = inbox.poll_messages(long_poll_s=long_poll_s)
        except Exception as e:
            kind = _error_kind(e)
            log.warning("telegram_reader.poll_failed", error=kind)
            now = clock()
            if kind not in announced or now - announced[kind] >= notice_interval_s:
                announced[kind] = now
                _notify(notifier, f"Navigator: Telegram reader not polling - {kind} "
                                  f"(this error is repeated at most once every "
                                  f"{_interval_text(notice_interval_s)})")
            sleep(error_backoff_s)
            continue
        for msg in messages:
            try:
                handle_operator_message(msg, act, notifier, chat_id)
            except Exception as e:
                # the message is consumed (offset persisted); the next one still gets handled
                log.warning("telegram_reader.handle_failed", error=type(e).__name__)
    return 0


def _install_sigterm():
    try:
        return signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: leave the handler alone
        return None


def _restore_sigterm(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, TypeError):
        pass


def main_reader() -> int:
    """Entry point of `python -m navigator telegram-reader`."""
    if os.getenv("NAVIGATOR_TELEGRAM_COMMANDS") == "1":
        print("navigator telegram-reader: refusing to start - NAVIGATOR_TELEGRAM_COMMANDS=1 "
              "makes the daemon poll getUpdates and Telegram allows one consumer per bot; "
              "unset it to run the reader", file=sys.stderr)
        return 2
    creds = load_telegram_credentials()
    if creds is None:
        print("navigator telegram-reader: no Telegram credentials "
              "(NAVIGATOR_TELEGRAM_BOT_TOKEN + NAVIGATOR_TELEGRAM_CHAT_ID or "
              "NAVIGATOR_NOTIFY_ENV_FILE)", file=sys.stderr)
        return 2
    token, chat_id = creds
    inbox = TelegramInbox(token, chat_id,
                          os.getenv("NAVIGATOR_TELEGRAM_OFFSET_PATH") or DEFAULT_OFFSET_PATH,
                          os.getenv("NAVIGATOR_TELEGRAM_INBOX_PATH") or DEFAULT_INBOX_PATH)
    act = HTTPACTClient(os.getenv("NAVIGATOR_ACT_BASE_URL") or DEFAULT_ACT_BASE_URL)
    notifier = make_notifier()
    previous = _install_sigterm()
    try:
        return run_reader(inbox, act, notifier, chat_id)
    except (KeyboardInterrupt, Terminated):
        return 0
    finally:
        _restore_sigterm(previous)
