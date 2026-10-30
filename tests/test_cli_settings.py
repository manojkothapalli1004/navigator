"""`navigator settings` (U2b): show the settings document next to what the daemon reports,
set/unset one hot field with optimistic versioning (actor 'operator'), refuse env-only and
invalid fields the way Settings.overlay does."""
import io

from navigator.cli import run_cli
from navigator.clients.act_client import DocVersionConflict, FakeACTClient

DOC = "navigator/settings"
STATUS = "navigator/status"


def _cli(act, *args):
    out = io.StringIO()
    rc = run_cli(["settings", *args], act=act, out=out)
    return rc, out.getvalue()


def test_show_absent():
    act = FakeACTClient()
    rc, text = _cli(act)
    assert rc == 0
    assert "settings document: absent" in text
    assert "daemon status: absent" in text
    assert "mode = ?  (no status)" in text
    assert act.docs == {}  # show never writes


def test_show_with_status():
    act = FakeACTClient()
    act.put_doc(DOC, {"mode": "execute"}, actor="ui")
    act.put_doc(STATUS, {"ts": "2026-10-30T12:00:00+00:00", "cycle": 7, "settings_version": 1,
                         "effective": {"mode": "dry_run", "digest_hour": None,
                                       "tick_interval_s": 900},
                         "env_only": {}, "settings_error": None,
                         "settings_rejected": ["colour: unknown"]}, actor="navigator-daemon")
    rc, text = _cli(act)
    assert rc == 0
    assert "settings document: version 1" in text
    assert "daemon status: cycle 7 at 2026-10-30T12:00:00+00:00, applied version 1" in text
    assert 'mode = "execute"  (document)' in text
    assert "tick_interval_s = 900  (daemon env/default)" in text
    assert "digest_hour = null  (daemon env/default)" in text
    assert "sdk_model = ?  (no status)" in text
    assert "rejected: colour: unknown" in text
    assert "settings error" not in text
    assert act.docs[DOC][1] == 1 and act.docs[STATUS][1] == 1


def test_show_reports_settings_error():
    act = FakeACTClient()
    act.put_doc(STATUS, {"cycle": 1, "ts": "t", "settings_version": None, "effective": {},
                         "settings_error": "ValueError: boom", "settings_rejected": []},
                actor="navigator-daemon")
    rc, text = _cli(act)
    assert rc == 0 and "settings error: ValueError: boom" in text
    assert "rejected:" not in text


def test_set_creates_then_increments():
    act = FakeACTClient()
    rc, text = _cli(act, "set", "mode", "execute")
    assert rc == 0
    assert 'navigator/settings version 1: mode = "execute"' in text
    assert "next cycle" in text
    assert act.docs[DOC] == ({"mode": "execute"}, 1, "operator")
    rc, text = _cli(act, "set", "digest_hour", "7")
    assert rc == 0 and "version 2: digest_hour = 7" in text
    body, version, actor = act.docs[DOC]
    assert version == 2 and actor == "operator"
    assert body == {"mode": "execute", "digest_hour": 7}
    assert isinstance(body["digest_hour"], int) and not isinstance(body["digest_hour"], bool)


def test_set_bool_float_and_tz():
    act = FakeACTClient()
    assert _cli(act, "set", "merge_tier_b_batch", "true")[0] == 0
    assert _cli(act, "set", "planner_throttle_hours", "1.5")[0] == 0
    assert _cli(act, "set", "decisions_tz", "Europe/London")[0] == 0
    assert _cli(act, "set", "sdk_model", "42")[0] == 0  # never parsed as a number
    body = act.docs[DOC][0]
    assert body["merge_tier_b_batch"] is True
    assert body["planner_throttle_hours"] == 1.5
    assert body["decisions_tz"] == "Europe/London" and body["sdk_model"] == "42"
    assert act.docs[DOC][1] == 4


def test_set_role_models():
    act = FakeACTClient()
    rc, text = _cli(act, "set", "sdk_role_models", "builder=claude-opus-5-5")
    assert rc == 0
    assert act.docs[DOC][0] == {"sdk_role_models": {"builder": "claude-opus-5-5"}}
    rc, _ = _cli(act, "set", "sdk_role_models", "planner=m1,builder=m2")
    assert rc == 0
    assert act.docs[DOC][0] == {"sdk_role_models": {"planner": "m1", "builder": "m2"}}
    rc, _ = _cli(act, "set", "sdk_role_models", "")
    assert rc == 0 and act.docs[DOC][0] == {"sdk_role_models": {}}


def test_set_role_models_bad_pair():
    act = FakeACTClient()
    rc, text = _cli(act, "set", "sdk_role_models", "builder")
    assert rc == 2
    assert "refused: sdk_role_models" in text and "Traceback" not in text
    assert DOC not in act.docs


def test_set_refuses_env_only():
    act = FakeACTClient()
    rc, text = _cli(act, "set", "allowed_commands", "x")
    assert rc == 2 and "refused: allowed_commands: env-only" in text
    assert DOC not in act.docs
    rc, text = _cli(act, "set", "colour", "blue")
    assert rc == 2 and "refused: colour: unknown" in text
    assert DOC not in act.docs


def test_set_refuses_bad_value():
    act = FakeACTClient()
    rc, text = _cli(act, "set", "tick_interval_s", "5")
    assert rc == 2 and text.startswith("refused: tick_interval_s")
    assert DOC not in act.docs
    rc, _ = _cli(act, "set", "digest_hour", "true")
    assert rc == 2 and DOC not in act.docs


def test_set_infinite_float_refused():
    act = FakeACTClient()
    for raw in ("inf", "-inf", "nan", "infinity"):
        rc, text = _cli(act, "set", "planner_throttle_hours", raw)
        assert rc == 2 and text.startswith("refused: planner_throttle_hours"), raw
    assert DOC not in act.docs


class _Conflicting(FakeACTClient):
    def put_doc(self, key, body, actor, expected_version=None):
        raise DocVersionConflict(4)


def test_set_conflict():
    act = _Conflicting()
    rc, text = _cli(act, "set", "mode", "execute")
    assert rc == 1
    assert "settings changed meanwhile (now version 4) - run it again" in text


def test_set_uses_expected_version():
    seen = []

    class _Spy(FakeACTClient):
        def put_doc(self, key, body, actor, expected_version=None):
            seen.append(expected_version)
            return super().put_doc(key, body, actor, expected_version)

    act = _Spy()
    assert _cli(act, "set", "mode", "execute")[0] == 0
    assert _cli(act, "set", "mode", "dry_run")[0] == 0
    assert _cli(act, "unset", "mode")[0] == 0
    assert seen == [0, 1, 2]


def test_unset():
    act = FakeACTClient()
    assert _cli(act, "set", "mode", "execute")[0] == 0
    rc, text = _cli(act, "unset", "mode")
    assert rc == 0 and "version 2: mode unset" in text
    assert act.docs[DOC] == ({}, 2, "operator")
    rc, text = _cli(act, "unset", "mode")
    assert rc == 0 and "mode is not set in the document" in text
    assert act.docs[DOC][1] == 2
    # no document at all: nothing written
    act2 = FakeACTClient()
    rc, text = _cli(act2, "unset", "mode")
    assert rc == 0 and "is not set" in text and act2.docs == {}


def test_bad_usage():
    act = FakeACTClient()
    for args in (("frob",), ("set", "mode"), ("set",), ("unset",), ("unset", "a", "b"),
                 ("set", "mode", "execute", "extra")):
        rc, text = _cli(act, *args)
        assert rc == 2 and text.startswith("usage: navigator settings"), args
    assert act.docs == {}


def test_settings_in_top_level_usage():
    out = io.StringIO()
    assert run_cli([], act=FakeACTClient(), out=out) == 2
    assert "settings" in out.getvalue()


class _StatusBroken(FakeACTClient):
    def get_doc(self, key):
        if key == STATUS:
            raise ConnectionError("act down at http://act.local/botSECRETTOKEN")
        return super().get_doc(key)


def test_show_status_read_error():
    act = _StatusBroken()
    act.put_doc(DOC, {"mode": "execute"}, actor="ui")
    rc, text = _cli(act)
    assert rc == 1
    assert "settings: ACT unreachable (ConnectionError)" in text
    assert "SECRETTOKEN" not in text
