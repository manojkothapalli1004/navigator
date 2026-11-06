import asyncio
import base64
import io
import json
import stat
import zipfile

import httpx
import pytest

from navigator.conductor import attachments as att
from navigator.conductor import server
from navigator.conductor.engine import Engine
from navigator.conductor.store import Store


async def _runner(prompt, sid, emit):
    return "s-1"


def _docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:r><w:t>A &amp; B</w:t></w:r></w:p>")
    return buf.getvalue()


def _b64(b):
    return base64.b64encode(b).decode()


async def _with(tmp_path, fn, configured=True, engine=None):
    eng = engine or Engine(Store(tmp_path / "c"), _runner)
    d = tmp_path / "att"
    app = server.build_app(eng, attachments_dir=d if configured else None)
    await eng.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
            return await fn(c, eng, d)
    finally:
        await eng.stop()


def test_saves_and_submits(tmp_path):
    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "cv.pdf", "data": _b64(b"%PDF-1.4 x"),
                                               "text": "my cv", "source": "telegram"})
        assert r.status_code == 202 and r.json()["kind"] == "user"
        [f] = list(d.iterdir())
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
        text = r.json()["text"]
        assert text.startswith("my cv\n\n[attachment: cv.pdf saved at " + str(f))
        assert "delegate" in text and "Read" in text and r.json()["source"] == "telegram"
    asyncio.run(_with(tmp_path, go))


def test_docx_text_copy(tmp_path):
    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "a.docx", "data": _b64(_docx()), "source": "web"})
        assert r.status_code == 202
        [t] = [p for p in d.iterdir() if p.name.endswith(".docx.txt")]
        assert t.read_text() == "A & B\n" and "Plain-text copy:" in r.json()["text"]
    asyncio.run(_with(tmp_path, go))


def _zip_with_members(n):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:t>a</w:t>")
        for i in range(n):
            z.writestr(f"m{i}", b"")
    return buf.getvalue()


@pytest.mark.parametrize("body", [
    {"name": "x.pdf", "data": "!!notb64", "source": "web"},
    {"name": "x.exe", "data": _b64(b"%PDF-1"), "source": "web"},
    {"name": "x.pdf", "data": _b64(b"hello"), "source": "web"},
    {"name": "x.docx", "data": _b64(b"%PDF-1.4"), "source": "web"},
    {"name": "x.docx", "data": _b64(_zip_with_members(2001)), "source": "web"},
    {"name": "x.txt", "data": _b64(b"a\x00b"), "source": "web"},
    {"name": "x.txt", "data": _b64(b"\xff\xfe"), "source": "web"},
    {"name": "x.txt", "data": "", "source": "web"},
    {"name": "x.pdf", "data": _b64(b"%PDF-1"), "source": "email"},
    {"name": "x.pdf", "data": _b64(b"%PDF-1"), "source": "approval"},
    {"name": "x.pdf", "data": _b64(b"%PDF-1"), "source": "web", "text": "t" * 19000},
    {"data": _b64(b"%PDF-1"), "source": "web"},
])
def test_refusals_save_nothing(tmp_path, body):
    async def go(c, eng, d):
        before = len(eng.store.history()) if hasattr(eng.store, "history") else None
        r = await c.post("/attachments", json=body)
        assert r.status_code == 422 and "error" in r.json()
        assert not d.exists() or list(d.iterdir()) == []
        if before is not None:
            assert len(eng.store.history()) == before
    asyncio.run(_with(tmp_path, go))


def test_not_json_and_bad_content_length(tmp_path):
    async def go(c, eng, d):
        assert (await c.post("/attachments", content=b"nope")).status_code == 422
        r = await c.post("/attachments", content=b"{}", headers={"content-length": "abc"})
        assert r.status_code == 422
    asyncio.run(_with(tmp_path, go))


def test_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "MAX_BYTES", 64)

    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "x.txt", "data": _b64(b"a" * 65), "source": "web"})
        assert r.status_code == 413
        r = await c.post("/attachments", content=b"{}", headers={"content-length": "999999"})
        assert r.status_code == 413
        offered = []

        async def gen():
            for i in range(50):
                offered.append(i)
                yield b"x" * 100_000
        r = await c.post("/attachments", content=gen())
        assert r.status_code == 413 and len(offered) < 50
        assert not d.exists() or list(d.iterdir()) == []
    asyncio.run(_with(tmp_path, go))


def test_work_runs_off_the_loop(tmp_path, monkeypatch):
    calls = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(fn)
        return await real(fn, *a, **k)
    monkeypatch.setattr(asyncio, "to_thread", spy)

    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "x.txt", "data": _b64(b"hi"), "source": "web"})
        assert r.status_code == 202 and calls
    asyncio.run(_with(tmp_path, go))


def test_symlinked_dir_503(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "att").symlink_to(real)

    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "x.txt", "data": _b64(b"hi"), "source": "web"})
        assert r.status_code == 503 and "error" in r.json() and list(real.iterdir()) == []
    asyncio.run(_with(tmp_path, go))


def test_submit_failure_cleans_up(tmp_path):
    class Boom(Engine):
        def submit(self, *a, **k):
            raise RuntimeError("boom")

    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "a.docx", "data": _b64(_docx()), "source": "web"})
        assert r.status_code == 500 and list(d.iterdir()) == []
    asyncio.run(_with(tmp_path, go, engine=Boom(Store(tmp_path / "c"), _runner)))


def test_no_collision(tmp_path):
    async def go(c, eng, d):
        body = {"name": "x.txt", "data": _b64(b"hi"), "source": "web"}
        assert (await c.post("/attachments", json=body)).status_code == 202
        assert (await c.post("/attachments", json=body)).status_code == 202
        assert len(list(d.iterdir())) == 2
    asyncio.run(_with(tmp_path, go))


def test_unconfigured_503(tmp_path):
    async def go(c, eng, d):
        r = await c.post("/attachments", json={"name": "x.txt", "data": _b64(b"hi"), "source": "web"})
        assert r.status_code == 503
    asyncio.run(_with(tmp_path, go, configured=False))


def test_no_logging_of_content(tmp_path, caplog):
    caplog.set_level("DEBUG")

    async def go(c, eng, d):
        data = _b64(b"secret body")
        await c.post("/attachments", json={"name": "private.txt", "data": data, "text": "captionxyz",
                                           "source": "web"})
        for rec in caplog.records:
            m = rec.getMessage()
            assert "private" not in m and "captionxyz" not in m and data not in m
    asyncio.run(_with(tmp_path, go))


def test_serve_wires_attachments_dir(tmp_path, monkeypatch):
    class _Stop(Exception):
        pass
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        raise _Stop
    monkeypatch.setattr(server, "build_app", spy)
    with pytest.raises(_Stop):
        import tempfile
        from pathlib import Path
        short = Path(tempfile.mkdtemp(dir="/tmp"))
        server.serve(short / "c.sock", tmp_path / "root", runner=_runner)
    assert seen["attachments_dir"] == att.ATTACH_DIR


def test_not_reachable_from_tools():
    from pathlib import Path
    base = Path(server.__file__).parent
    for name in ("tools.py", "helpers.py", "gate.py"):
        src = (base / name).read_text()
        assert "attachments" not in src and "/attachments" not in src


def test_helper_can_read_attachment_path():
    from pathlib import Path
    from navigator.conductor import gate
    assert gate.gated_reason("Read", {"file_path": str(att.ATTACH_DIR / "x.pdf")},
                             Path("/Users/Manoj/calwbot")) is None
