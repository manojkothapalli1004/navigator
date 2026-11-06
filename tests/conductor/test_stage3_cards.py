import asyncio

from tests.conductor.test_engine import FakeRunner, _eng, _reng, aio


@aio
async def test_engine_note_appends_quietly(tmp_path):  # test 14
    runner = FakeRunner(); sent = []
    eng = _reng(tmp_path, runner, sent); await eng.start()
    eng.note("hello there")
    eng.note("tg too", via="telegram")
    await asyncio.sleep(0.05)
    notes = [e for e in eng.store.history(limit=100) if e.get("kind") == "note"]
    assert [n["text"] for n in notes] == ["hello there", "tg too"]
    assert not [e for e in eng.store.history(limit=100) if e.get("kind") == "user"]
    assert runner.calls == [] and eng.queue_len == 0
    assert sent == ["tg too"]
    await eng.stop()
