"""decisions.py reads digest_hour / decisions_nudges / decisions_tz from Settings (U2b):
a non-None field beats the environment variable, None keeps today's env path, and
__main__._decisions_step passes settings to decisions_tick only when one of those fields
is set (the two-argument call stays otherwise)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import navigator.__main__ as main_mod
from navigator.clients.act_client import FakeACTClient
from navigator.config import Settings
from navigator.contracts.enums import ApprovalGate, ProposalStatus, RunPhase
from navigator.contracts.proposal import Proposal
from navigator.contracts.run import Run
from navigator.observability import decisions, telegram_inbox

NY = ZoneInfo("America/New_York")
AUDITED: set[str] = set()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_TZ", "America/New_York")
    monkeypatch.setenv("NAVIGATOR_DECISIONS_STATE_PATH", str(tmp_path / "decisions_state.json"))
    monkeypatch.setenv("NAVIGATOR_MANUAL_DECISIONS_PATH", str(tmp_path / "manual_decisions.json"))
    monkeypatch.delenv("NAVIGATOR_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("NAVIGATOR_DECISIONS_NUDGES", raising=False)
    AUDITED.clear()
    monkeypatch.setattr(telegram_inbox, "is_audited", lambda act, run_id: run_id in AUDITED)


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _ny(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


def _merge_run(act, title):
    """An audited run at the merge gate whose proposal is approved (like test_decisions)."""
    p = act.create_proposal(Proposal(origin="periodic", proposal_origin_hash=f"h-{title}",
                                     title=title, rationale="r", repo="/repo",
                                     base_commit_sha="abc", intended_changes="x",
                                     affected_files=["a.py"]))
    act.set_proposal_status(p.id, ProposalStatus.approved)
    run = act.create_run(Run(proposal_id=p.id, base_commit_sha="abc", branch=f"nav/{p.id}",
                             phase=RunPhase.awaiting_review))
    act.create_approval(target=run.id, gate=ApprovalGate.merge)
    AUDITED.add(run.id)
    return run


def _seed(path, digest, first_seen=None):
    assert decisions.write_state(path, {"first_seen": first_seen or {}, "nudged": {},
                                        "digest": digest})


# ---- the three readers ------------------------------------------------------------

def test_digest_hour_from_settings_beats_env(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_DIGEST_HOUR", "8")
    assert decisions.digest_hour(Settings(digest_hour=6)) == 6


def test_digest_hour_env_when_field_none(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_DIGEST_HOUR", "9")
    assert decisions.digest_hour(Settings()) == 9
    assert decisions.digest_hour() == 9
    assert decisions.digest_hour(None) == 9
    monkeypatch.delenv("NAVIGATOR_DIGEST_HOUR")
    assert decisions.digest_hour(Settings()) == decisions.DIGEST_HOUR


def test_nudges_and_tz_from_settings(monkeypatch):
    assert decisions.nudges_enabled(Settings(decisions_nudges=False)) is False
    assert decisions.nudges_enabled(Settings()) is True
    monkeypatch.setenv("NAVIGATOR_DECISIONS_NUDGES", "0")
    assert decisions.nudges_enabled(Settings(decisions_nudges=True)) is True
    assert decisions.nudges_enabled(Settings()) is False
    assert decisions.local_tz(Settings(decisions_tz="Europe/London")).key == "Europe/London"
    assert decisions.local_tz(Settings()).key == "America/New_York"
    assert decisions.local_tz().key == "America/New_York"


def test_bad_env_still_raises_when_field_none(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_DIGEST_HOUR", "x")
    with pytest.raises(ValueError, match="NAVIGATOR_DIGEST_HOUR"):
        decisions.digest_hour(Settings())
    # a set field is never affected by a bad environment value
    assert decisions.digest_hour(Settings(digest_hour=6)) == 6


# ---- the tick ---------------------------------------------------------------------

def test_tick_uses_settings_digest_hour(tmp_path):
    act = FakeACTClient()
    _merge_run(act, "Quota: pause builds")
    now = _ny(25, 7, 5)
    # NEGATIVE: settings=None -> the env path: 7am is quiet with the default hour 8
    rec0, state0 = _Recorder(), tmp_path / "s0.json"
    _seed(state0, "2026-10-24")
    out = decisions.decisions_tick(act, rec0, now=now, state_file=state0, settings=None)
    assert out == {"quiet": True, "items": 1} and rec0.sent == []
    assert decisions.read_state(state0)["digest"] == "2026-10-24"
    # Settings() with every decisions field None is the env path too
    rec1, state1 = _Recorder(), tmp_path / "s1.json"
    _seed(state1, "2026-10-24")
    out = decisions.decisions_tick(act, rec1, now=now, state_file=state1, settings=Settings())
    assert out == {"quiet": True, "items": 1} and rec1.sent == []
    rec, state = _Recorder(), tmp_path / "s.json"
    _seed(state, "2026-10-24")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   settings=Settings(digest_hour=7))
    assert out == {"items": 1, "digest_sent": True, "nudged": 0}
    assert len(rec.sent) == 1
    assert rec.sent[0].startswith("Morning digest") and "merge Quota" in rec.sent[0]
    assert decisions.read_state(state)["digest"] == "2026-10-25"
    # once per day: the next tick in the same hour sends nothing more
    decisions.decisions_tick(act, rec, now=_ny(25, 7, 20), state_file=state,
                             settings=Settings(digest_hour=7))
    assert len(rec.sent) == 1


def test_tick_uses_settings_tz(tmp_path):
    act = FakeACTClient()
    _merge_run(act, "Quota: pause builds")
    now = _ny(25, 7, 5)  # 11:05 in London: past the default digest hour, not quiet
    rec, state = _Recorder(), tmp_path / "s.json"
    _seed(state, "2026-10-24")
    out = decisions.decisions_tick(act, rec, now=now, state_file=state,
                                   settings=Settings(decisions_tz="Europe/London"))
    assert out == {"items": 1, "digest_sent": True, "nudged": 0}
    assert rec.sent[0].startswith("Morning digest")
    assert decisions.read_state(state)["digest"] == "2026-10-25"


def test_tick_nudges_follow_settings(tmp_path):
    act = FakeACTClient()
    run = _merge_run(act, "Quota: pause builds")
    now = _ny(24, 10)
    seen = {f"merge:{run.id}": (now - timedelta(hours=5)).isoformat()}
    for settings, expected in ((Settings(decisions_nudges=False), 0), (Settings(), 1)):
        rec, state = _Recorder(), tmp_path / f"s{expected}.json"
        _seed(state, "2026-10-24", first_seen=seen)
        out = decisions.decisions_tick(act, rec, now=now, state_file=state, settings=settings)
        assert out == {"items": 1, "digest_sent": False, "nudged": expected}
        assert len(rec.sent) == expected
        if expected:
            assert rec.sent[0].startswith("Reminder")


# ---- the daemon step --------------------------------------------------------------

def test_decisions_step_passes_settings_only_when_set(monkeypatch):
    calls = []

    def recorder(*a, **kw):
        calls.append((a, kw))
        return {"items": 0, "digest_sent": False, "nudged": 0}

    monkeypatch.setattr(decisions, "decisions_tick", recorder)
    act, notifier = FakeACTClient(), _Recorder()
    summary = {}
    main_mod._decisions_step(SimpleNamespace(act=act, notifier=notifier, settings=Settings()),
                             summary)
    assert calls[-1] == ((act, notifier), {})
    assert "decisions_error" not in summary
    s = Settings(digest_hour=6)
    main_mod._decisions_step(SimpleNamespace(act=act, notifier=notifier, settings=s), {})
    assert calls[-1][0] == (act, notifier) and calls[-1][1]["settings"] is s
    main_mod._decisions_step(SimpleNamespace(act=act, notifier=notifier), {})
    assert calls[-1] == ((act, notifier), {})
    fleet = SimpleNamespace(act=act, notifier=notifier,
                            navigators=[SimpleNamespace(settings=s), SimpleNamespace()])
    summary = {}
    main_mod._decisions_step(fleet, summary)
    assert calls[-1][0] == (act, notifier) and calls[-1][1]["settings"] is s
    assert "decisions_error" not in summary
    # the other decisions fields count too
    for other in (Settings(decisions_nudges=False), Settings(decisions_tz="Europe/London")):
        main_mod._decisions_step(SimpleNamespace(act=act, notifier=notifier, settings=other), {})
        assert calls[-1][1]["settings"] is other
    assert len(calls) == 6
