"""doctor's settings-document report (U2b, cli._check_settings_doc): absent / unreadable /
present, the plist keys the document overrides, the keys the daemon refused. Informational
only: it never makes doctor fail and never writes."""
import io

from navigator import cli
from navigator.cli import run_cli
from navigator.clients.act_client import FakeACTClient
from tests.test_cli_doctor import _git_repo, _plist

DOC = "navigator/settings"
STATUS = "navigator/status"


def _check(act, env):
    out = io.StringIO()
    cli._check_settings_doc(act, env, out)
    return out.getvalue()


class _Broken(FakeACTClient):
    def get_doc(self, key):
        raise ConnectionError("http://act.local/botSECRETTOKEN")


def test_no_act_prints_nothing():
    assert _check(None, {"NAVIGATOR_MODE": "execute"}) == ""


def test_absent():
    act = FakeACTClient()
    text = _check(act, {"NAVIGATOR_MODE": "execute"})
    assert "info settings document absent (daemon runs on its environment)" in text
    assert "WARN" not in text
    assert act.docs == {}


def test_unreadable():
    text = _check(_Broken(), {})
    assert "WARN settings document unreadable: ConnectionError" in text
    assert "SECRETTOKEN" not in text


def test_override_warns_when_plist_sets_the_key():
    act = FakeACTClient()
    act.put_doc(DOC, {"merge_tier_a_auto": True}, actor="ui")
    text = _check(act, {"NAVIGATOR_MERGE_TIER_A_AUTO": "0"})
    assert "ok   settings document version 1" in text
    assert ("WARN the settings document overrides NAVIGATOR_MERGE_TIER_A_AUTO "
            "(plist value ignored)") in text
    assert act.docs[DOC][1] == 1  # never written


def test_no_override_line_without_the_env_key():
    # NEGATIVE: the plist does not set the key, so nothing is overridden
    act = FakeACTClient()
    act.put_doc(DOC, {"merge_tier_a_auto": True}, actor="ui")
    text = _check(act, {"NAVIGATOR_MODE": "execute"})
    assert "ok   settings document version 1" in text
    assert "overrides" not in text and "WARN" not in text


def test_role_models_override_names_each_role_key():
    act = FakeACTClient()
    act.put_doc(DOC, {"sdk_role_models": {"builder": "m"}}, actor="ui")
    text = _check(act, {"NAVIGATOR_SDK_MODEL_BUILDER": "x", "NAVIGATOR_SDK_MODEL_PLANNER": "y",
                        "NAVIGATOR_SDK_MODEL": "z"})
    assert "overrides NAVIGATOR_SDK_MODEL_BUILDER (plist value ignored)" in text
    assert "overrides NAVIGATOR_SDK_MODEL_PLANNER (plist value ignored)" in text
    assert "overrides NAVIGATOR_SDK_MODEL " not in text  # sdk_model is not in the document
    assert "NAVIGATOR_SDK_MODEL_VERIFIER" not in text


def test_rejected_keys_from_status():
    act = FakeACTClient()
    act.put_doc(DOC, {"colour": "blue"}, actor="ui")
    act.put_doc(STATUS, {"settings_rejected": ["colour: unknown"]}, actor="navigator-daemon")
    text = _check(act, {})
    assert "WARN settings document key refused: colour: unknown" in text


def test_doctor_never_fails_because_of_the_settings_document(tmp_path, monkeypatch):
    # NEGATIVE (C5): overrides, refused keys and an unreadable document are WARN/info only
    monkeypatch.setattr(cli, "_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, {"NAVIGATOR_REPO": str(repo), "NAVIGATOR_MERGE_TIER_A_AUTO": "0"})
    act = FakeACTClient()
    act.put_doc(DOC, {"merge_tier_a_auto": True}, actor="ui")
    act.put_doc(STATUS, {"settings_rejected": ["colour: unknown"]}, actor="navigator-daemon")
    out = io.StringIO()
    assert run_cli(["doctor", "--plist", str(plist)], act=act, out=out) == 0
    text = out.getvalue()
    assert "overrides NAVIGATOR_MERGE_TIER_A_AUTO" in text
    assert "key refused: colour: unknown" in text
    assert "doctor: ok" in text
    assert act.docs[DOC][1] == 1 and act.docs[STATUS][1] == 1
    out = io.StringIO()
    assert run_cli(["doctor", "--plist", str(plist)], act=_Broken(), out=out) == 0
    text = out.getvalue()
    assert "WARN settings document unreadable: ConnectionError" in text
    assert "doctor: ok" in text and "SECRETTOKEN" not in text
    out = io.StringIO()
    assert run_cli(["doctor", "--plist", str(plist)], act=None, out=out) == 0
    assert "settings document" not in out.getvalue()
