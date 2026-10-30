"""merge_tiers.batch_enabled(act) (U2b): the settings document's merge_tier_b_batch wins
over NAVIGATOR_MERGE_TIER_B_BATCH in the Telegram process; any problem (no document, no or
non-bool key, a raising get_doc) falls back to the environment, and 'merge all' honours
the document. The BATCH_OFF reply text is unchanged."""
import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.observability.telegram_inbox import TelegramCommand, apply_commands
from navigator.runtime.merge_tiers import batch_enabled
from tests.test_telegram_inbox import _Recorder
from tests.test_telegram_shorthand import BATCH_OFF, _tiered

DOC = "navigator/settings"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MODE", "execute")
    monkeypatch.delenv("NAVIGATOR_MERGE_IN_DRY_RUN", raising=False)
    monkeypatch.delenv("NAVIGATOR_MERGE_TIER_B_BATCH", raising=False)


def _act(value):
    act = FakeACTClient()
    act.put_doc(DOC, {"merge_tier_b_batch": value}, actor="ui")
    return act


def test_doc_true_beats_env_unset():
    assert batch_enabled(_act(True)) is True


def test_doc_false_beats_env_true(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    assert batch_enabled(_act(False)) is False


def test_missing_doc_uses_env(monkeypatch):
    act = FakeACTClient()
    assert batch_enabled(act) is False
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    assert batch_enabled(act) is True
    # a document without the key is the same as no document
    act.put_doc(DOC, {"mode": "execute"}, actor="ui")
    assert batch_enabled(act) is True
    monkeypatch.delenv("NAVIGATOR_MERGE_TIER_B_BATCH")
    assert batch_enabled(act) is False


def test_non_bool_value_uses_env(monkeypatch):
    for bad in ("true", 1, 0, None, [True], {"on": True}):
        act = _act(bad)
        monkeypatch.delenv("NAVIGATOR_MERGE_TIER_B_BATCH", raising=False)
        assert batch_enabled(act) is False, bad
        monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
        assert batch_enabled(act) is True, bad


class _Broken(FakeACTClient):
    def get_doc(self, key):
        raise RuntimeError("ACT down at http://act.local/botSECRETTOKEN")


def test_get_doc_raises_uses_env(monkeypatch):
    # NEGATIVE: never raises
    assert batch_enabled(_Broken()) is False
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    assert batch_enabled(_Broken()) is True
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "maybe")
    assert batch_enabled(_Broken()) is False


def test_no_act_is_env(monkeypatch):
    assert batch_enabled() is False
    assert batch_enabled(None) is False
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    assert batch_enabled() is True
    assert batch_enabled(None) is True
    # an object without get_doc is treated like no act
    assert batch_enabled(object()) is True
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "maybe")
    assert batch_enabled() is False


def test_merge_all_uses_doc():
    act = _act(True)
    a = _tiered(act, "N5b: inbox", "B")
    b = _tiered(act, "D1: docs", "A")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results[0]["applied"] is True and results[0]["reason"] is None
    assert sorted(results[0]["approved"]) == sorted([a.id, b.id])
    assert len(rec.sent) == 1 and rec.sent[0] != BATCH_OFF
    assert "2 merge(s) approved" in rec.sent[0]


def test_merge_all_off_by_doc_despite_env(monkeypatch):
    monkeypatch.setenv("NAVIGATOR_MERGE_TIER_B_BATCH", "1")
    act = _act(False)
    _tiered(act, "N5b: inbox", "B")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results == [{"run_id": None, "action": "merge", "applied": False,
                        "reason": "batch off"}]
    assert rec.sent == [BATCH_OFF]


def test_merge_all_without_doc_and_env_is_off():
    act = FakeACTClient()
    _tiered(act, "N5b: inbox", "B")
    rec = _Recorder()
    results = apply_commands(act, [TelegramCommand("merge", None, 1, batch=True)], rec)
    assert results[0]["reason"] == "batch off"
    assert rec.sent == [BATCH_OFF]
