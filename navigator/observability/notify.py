import os
import re
from pathlib import Path
from typing import Protocol
import httpx


class Notifier(Protocol):
    # reply_markup (B4): a Telegram inline keyboard for the message, or None
    def send(self, text: str, reply_markup: dict | None = None) -> None: ...


class NullNotifier:
    def send(self, text: str, reply_markup: dict | None = None) -> None:
        pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=10.0)

    def send(self, text: str, reply_markup: dict | None = None) -> None:
        # best-effort: an unreachable Telegram must never fail a run or a cycle
        body: dict = {"chat_id": self.chat_id, "text": text[:4000]}
        if reply_markup is not None:  # only when given: the plain request stays byte-identical
            body["reply_markup"] = reply_markup
        try:
            self.client.post(self.url, json=body)
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


_RESULT_LINE = re.compile(r"\d+ (passed|failed|error)")
_PROGRESS_DONE = re.compile(r"\[\s*100%\]")


def format_verification(log: list[dict]) -> str:
    if not log:
        return "verification: (none recorded)"
    lines = ["verification:"]
    for c in log:
        if c.get("suite") == "exercise":
            # the run's new tests on the BASE code: failing there is the good outcome (2026-11-02)
            verdict = {True: "failed as expected", False: "passed on the base — the tests don't exercise the change",
                       None: "couldn't tell"}.get(c.get("exercised"), "couldn't tell")
            lines.append(f"  exercise check (new tests run against the base): {verdict}")
            continue
        tail = (c.get("output_tail") or "").strip().splitlines()
        # pytest's result line beats trailing stderr noise (uv's venv setup)
        result = next((l for l in reversed(tail) if _RESULT_LINE.search(l)), None)
        if result is None and any(_PROGRESS_DONE.search(l) for l in tail):
            result = "tests passed (100%)" if c.get("returncode") == 0 else "tests failed"
        if result is None:
            result = tail[-1] if tail else ""
        lines.append(f"  $ {c.get('argv')}  rc={c.get('returncode')}  {result.strip()}")
    return "\n".join(lines)


class NotifyingACT:
    """ACT client wrapper that turns run events into operator messages."""

    def __init__(self, inner, notifier: Notifier):
        self.inner = inner
        self.notifier = notifier
        # (run_id, reason): a new block reason on the same run is announced once
        self._blocked_announced: set[tuple[str, str]] = set()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def emit_event(self, event) -> None:
        self.inner.emit_event(event)
        text = self._message(event.kind, event.payload)
        if not text:
            return
        markup = self._markup(event.kind, event.payload)
        if markup is None:
            self.notifier.send(text)
            return
        try:
            self.notifier.send(text, reply_markup=markup)
        except TypeError:
            # a notifier whose send takes only the text (older fakes, other tools): the
            # message still goes out, without the button
            self.notifier.send(text)

    @staticmethod
    def _markup(kind: str, p: dict) -> dict | None:
        """The inline keyboard for a kind, or None: run.declined gets one Fix button whose
        callback data 'fix:<run-id>' telegram_inbox._card_tap turns into 'fix <run-id>'."""
        run_id = p.get("run_id")
        if kind == "run.declined" and isinstance(run_id, str) and run_id:
            return {"inline_keyboard": [[{"text": "Fix", "callback_data": f"fix:{run_id}"}]]}
        return None

    def _message(self, kind: str, p: dict) -> str | None:
        run_id = p.get("run_id", "?")
        if kind == "run.awaiting_review":
            verified = p.get("verified_sha")
            verified_line = f"verified {verified[:12]}\n" if verified else ""
            tier_line = f"tier: {p.get('merge_tier')}\n" if p.get("merge_tier") in ("A", "B", "C") else ""
            return (f"Navigator: {run_id} is ready for review\n{p.get('title', '')}\n"
                    f"{verified_line}"
                    f"{tier_line}"
                    f"{_diff_summary(p.get('diff', ''))}\n"
                    f"{format_verification(p.get('verification') or [])}\n"
                    f"review: python -m navigator diff {run_id}\n"
                    f"merge:  python -m navigator merge {run_id}\n"
                    f"reject: python -m navigator reject {run_id}\n"
                    f"telegram: /merge {run_id}  (after audit)")
        if kind == "run.merge_blocked":
            reason = p.get("reason", "") or ""
            key = (run_id, reason)
            if key in self._blocked_announced:
                return None
            self._blocked_announced.add(key)
            return f"Navigator: merge of {run_id} is waiting \u2014 {reason}"
        if kind == "run.failed":
            return f"Navigator: {run_id} FAILED \u2014 {p.get('error') or 'see outcome'}"
        if kind == "run.declined":
            return (f"Navigator: {run_id} declined (reason: {p.get('reason_code') or 'none given'}); "
                    f"worktree kept for inspection\nfix: tap Fix or reply 'fix {run_id}'")
        if kind == "run.completed":
            text = f"Navigator: {run_id} merged"
            if p.get("auto") and p.get("origin") == "site-checker":
                text = (f"Navigator: {run_id} fixed site fault: {p.get('title') or 'untitled'}"
                        " — merged automatically (checker re-check passed, audited)")
            elif p.get("auto"):
                text += " automatically (tier A: tests/docs only, audited, verification green)"
            if p.get("revert"):
                text += f"\nrevert: {p['revert']}"
            return text
        if kind == "run.deferred":
            # N16: the payload carries the error CLASS and a reset time, never SDK text
            return (f"Navigator: {run_id} deferred - {p.get('error') or 'quota exhausted'}; "
                    f"resumes after {p.get('resets_at') or 'the limit lifts'}")
        return None
