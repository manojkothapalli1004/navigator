"""The telegram_inbox module docstring names the real TelegramInbox entry points.

A docstring that names a method which does not exist (or omits the one the
always-on reader actually calls) misleads planners and builders; this guards
against that drift.
"""
import re

from navigator.observability import telegram_inbox
from navigator.observability.telegram_inbox import TelegramInbox


def _named_polls(doc: str) -> set[str]:
    return set(re.findall(r"\b(poll_[a-z_]+)\b", doc))


def test_docstring_names_only_existing_poll_entry_points():
    named = _named_polls(telegram_inbox.__doc__ or "")
    assert named, "module docstring should describe the poll entry points"
    for name in named:
        assert callable(getattr(TelegramInbox, name, None)), f"{name} is stale"


def test_docstring_names_the_reader_entry_point_and_the_daemon_poll():
    doc = telegram_inbox.__doc__ or ""
    assert "poll_updates(long_poll_s)" in doc
    assert "poll_once" in doc
    assert "poll_messages" in doc
