"""build_navigator wires the Telegram inbox for single-repo and Fleet daemons, and
_telegram_step tells the operator about a polling failure once per error kind."""
import subprocess
import types

import httpx
import pytest

import navigator.__main__ as m
from navigator.clients.act_client import FakeACTClient
from navigator.config import Settings, effective_repos
from navigator.observability.notify import NotifyingACT
from navigator.observability.telegram_inbox import (CONFLICT_MESSAGE, FETCH_FAILED_MESSAGE,
                                                    TelegramInbox)
from navigator.runtime.fleet import Fleet
from navigator.runtime.loop import Navigator

CHAT = "4242"
_SETTINGS = types.SimpleNamespace(mode="execute", driver="fake")


def _git_repo(path):
    path.mkdir(parents=True)
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def _settings(tmp_path, repo):
    return Settings(repo=str(repo), driver="fake", worktree_root=str(tmp_path / "wt"),
                    lock_path=str(tmp_path / "nav.lock"),
                    outcome_path=str(tmp_path / "outcomes.jsonl"))


@pytest.fixture
def telegram_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_COMMANDS", "1")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_BOT_TOKEN", "SECRETTOKEN")
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_OFFSET_PATH", str(tmp_path / "offset"))
    monkeypatch.setenv("NAVIGATOR_TELEGRAM_INBOX_PATH", str(tmp_path / "inbox.jsonl"))
    monkeypatch.delenv("NAVIGATOR_ACT_BASE_URL", raising=False)
    return tmp_path


# ---- build_navigator wiring -------------------------------------------------------

def test_single_repo_navigator_gets_inbox_and_the_shared_act(telegram_env):
    repo = _git_repo(telegram_env / "repo")
    nav = m.build_navigator(_settings(telegram_env, repo))
    assert isinstance(nav, Navigator)
    assert isinstance(nav.telegram_inbox, TelegramInbox)
    assert nav.telegram_inbox.chat_id == CHAT
    assert nav.telegram_inbox.offset_path == telegram_env / "offset"
    assert nav.telegram_inbox.inbox_path == telegram_env / "inbox.jsonl"
    assert nav.telegram_act is nav.act
    assert isinstance(nav.telegram_act, NotifyingACT)
    assert isinstance(nav.telegram_act.inner, FakeACTClient)


def test_fleet_gets_inbox_and_the_act_shared_by_every_repo(telegram_env, monkeypatch):
    repos = [_git_repo(telegram_env / f"repo{i}") for i in (1, 2)]
    configs = [effective_repos(_settings(telegram_env, r))[0] for r in repos]
    monkeypatch.setattr(m, "effective_repos", lambda settings: list(configs))
    target = m.build_navigator(_settings(telegram_env, repos[0]))
    assert isinstance(target, Fleet) and len(target.navigators) == 2
    assert isinstance(target.telegram_inbox, TelegramInbox)
    assert target.telegram_inbox.chat_id == CHAT
    assert target.telegram_act is target.navigators[0].act
    assert all(nav.act is target.telegram_act for nav in target.navigators)
    assert isinstance(target.telegram_act, NotifyingACT)


def test_inbox_is_off_by_default(telegram_env, monkeypatch):
    monkeypatch.delenv("NAVIGATOR_TELEGRAM_COMMANDS", raising=False)
    repo = _git_repo(telegram_env / "repo")
    nav = m.build_navigator(_settings(telegram_env, repo))
    assert nav.telegram_inbox is None
    assert nav.telegram_act is nav.act


# ---- _telegram_step error reporting ------------------------------------------------

class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class _FakeNav:
    def __init__(self, inbox, act=None):
        self.calls = []
        self.last_snapshot_chars = 0
        self.notifier = _Recorder()
        self.act = act or FakeACTClient()
        self.telegram_inbox = inbox

    def periodic_tick(self):
        return []

    def approval_poll(self):
        return []

    def merge_poll(self):
        self.calls.append("merge_poll")
        return []


def _inbox(tmp_path, handler):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "offset").write_text("0")
    return TelegramInbox("SECRETTOKEN", CHAT, tmp_path / "offset", tmp_path / "inbox.jsonl",
                         client=httpx.Client(transport=httpx.MockTransport(handler)),
                         clock=lambda: 1760000600)


def _conflict(req):
    return httpx.Response(409, json={"ok": False, "error_code": 409,
                                     "description": "Conflict: terminated by other getUpdates"})


def test_409_is_reported_to_the_operator_once_across_three_cycles(tmp_path):
    nav = _FakeNav(_inbox(tmp_path, _conflict))
    summaries = [m._cycle(nav, _SETTINGS) for _ in range(3)]
    assert [s["telegram_error"] for s in summaries] == [f"TelegramError: {CONFLICT_MESSAGE}"] * 3
    assert all("error" not in s for s in summaries)   # the cycle itself did not fail
    assert nav.calls == ["merge_poll"] * 3
    assert len(nav.notifier.sent) == 1
    assert CONFLICT_MESSAGE in nav.notifier.sent[0]
    assert "SECRETTOKEN" not in nav.notifier.sent[0]
    assert "api.telegram.org" not in nav.notifier.sent[0]
    assert (tmp_path / "offset").read_text() == "0"


def test_each_distinct_error_kind_is_reported_once_and_recovery_rearms(tmp_path):
    script = ["409", "409", "down", "down", "ok", "409", "ok", "ok"]

    def handler(req):
        step = script.pop(0)
        if step == "409":
            return _conflict(req)
        if step == "down":
            raise httpx.ConnectError(f"cannot reach {req.url}")
        return httpx.Response(200, json={"ok": True, "result": []})
    nav = _FakeNav(_inbox(tmp_path, handler))
    summaries = [m._cycle(nav, _SETTINGS) for _ in range(8)]
    assert ["telegram_error" in s for s in summaries] == [True, True, True, True,
                                                          False, True, False, False]
    assert summaries[4]["telegram_commands"] == 0
    sent = nav.notifier.sent
    assert len(sent) == 3
    assert CONFLICT_MESSAGE in sent[0]
    assert FETCH_FAILED_MESSAGE in sent[1]
    assert CONFLICT_MESSAGE in sent[2]
    assert not any("SECRETTOKEN" in t or "api.telegram.org" in t for t in sent)
    assert nav.calls == ["merge_poll"] * 8


def test_non_telegram_error_is_reported_by_class_name_only():
    class _Boom:
        def poll_once(self):
            raise ValueError("https://api.telegram.org/botSECRETTOKEN/getUpdates")
    nav = _FakeNav(_Boom())
    for _ in range(2):
        m._cycle(nav, _SETTINGS)
    assert nav.calls == ["merge_poll"] * 2
    assert len(nav.notifier.sent) == 1
    assert "ValueError" in nav.notifier.sent[0]
    assert "SECRETTOKEN" not in nav.notifier.sent[0]
    assert "api.telegram.org" not in nav.notifier.sent[0]


def test_notifier_failure_while_reporting_does_not_break_the_cycle(tmp_path):
    class _Dead:
        def send(self, text):
            raise RuntimeError("telegram down")
    nav = _FakeNav(_inbox(tmp_path, _conflict))
    nav.notifier = _Dead()
    summary = m._cycle(nav, _SETTINGS)
    assert "error" not in summary
    assert summary["telegram_error"] == f"TelegramError: {CONFLICT_MESSAGE}"
    assert nav.calls == ["merge_poll"]
