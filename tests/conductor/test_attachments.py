import io
import re
import stat
import zipfile

import pytest

from navigator.conductor import attachments as att
from navigator.conductor.attachments import AttachmentError


def _docx(body: str = "<w:p><w:r><w:t>A &amp; B</w:t></w:r></w:p><w:p><w:r><w:t>second</w:t></w:r></w:p>",
          extra=0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
        for i in range(extra):
            z.writestr(f"x/{i}", b"")
    return buf.getvalue()


@pytest.mark.parametrize("raw,want", [
    ("../../etc/passwd.pdf", ("passwd", ".pdf")),
    ("C:\\x\\cv.DOCX", ("cv", ".docx")),
    ("a b\x00c.PDF", ("a_b_c", ".pdf")),
    (".pdf", ("file", ".pdf")),
])
def test_sanitize_name(raw, want):
    assert att.sanitize_name(raw) == want


@pytest.mark.parametrize("raw", ["evil.exe", "noext", "x.pdf.exe", "a.svg"])
def test_sanitize_refuses(raw):
    with pytest.raises(AttachmentError) as e:
        att.sanitize_name(raw)
    assert e.value.status == 422


def test_long_stem_capped():
    assert len(att.sanitize_name("a" * 300 + ".txt")[0]) <= 60


def test_stored_name_shape():
    n = att.stored_name("cv", ".pdf")
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", n) and "/" not in n and n.endswith("-cv.pdf")


def test_check_content_pdf_txt():
    att.check_content(".pdf", b"%PDF-1.4 x")
    for ext, data in ((".pdf", b"hello"), (".txt", b"a\x00b"), (".md", b"\xff\xfe\xfa")):
        with pytest.raises(AttachmentError) as e:
            att.check_content(ext, data)
        assert e.value.status == 422
    att.check_content(".md", "héllo".encode())


def test_docx_text_and_check():
    assert att.docx_text(_docx()) == "A & B\nsecond\n"
    assert att.check_content(".docx", _docx()) == "A & B\nsecond\n"


def test_docx_refusals():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.xml", "x")
    for bad in (b"%PDF-1.4", buf.getvalue(), _docx(extra=2001), b"PK\x03\x04garbage"):
        with pytest.raises(AttachmentError) as e:
            att.check_content(".docx", bad)
        assert e.value.status == 422


def test_zip_bomb_bounded(monkeypatch):
    monkeypatch.setattr(att, "MAX_ZIP_BYTES", 1024)
    with pytest.raises(AttachmentError) as e:
        att.check_content(".docx", _docx("<w:t>a</w:t>" * 2000))
    assert e.value.status == 422
    asked = []
    real = zipfile.ZipExtFile.read
    monkeypatch.setattr(zipfile.ZipExtFile, "read", lambda self, n=-1: (asked.append(n), real(self, n))[1])
    monkeypatch.setattr(att, "MAX_ZIP_BYTES", 10**6)
    att.docx_text(_docx())
    assert asked and all(n == 10**6 + 1 for n in asked)


def test_save_modes_and_no_overwrite(tmp_path):
    d = tmp_path / "att"
    p, t = att.save(d, "20260101-000000-abcdef-cv.pdf", b"%PDF-x")
    assert t is None and stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        att.save(d, p.name, b"%PDF-y")
    assert p.read_bytes() == b"%PDF-x"


def test_save_docx_writes_text_copy_and_cleans_up(tmp_path):
    d = tmp_path / "att"
    p, t = att.save(d, "20260101-000000-abcdef-cv.docx", _docx(), text="hi\n")
    assert t.name == p.name + ".txt" and t.read_text() == "hi\n" and stat.S_IMODE(t.stat().st_mode) == 0o600
    (d / "20260101-000000-abcdef-b.docx.txt").write_text("taken")
    with pytest.raises(FileExistsError):
        att.save(d, "20260101-000000-abcdef-b.docx", _docx(), text="x")
    assert not (d / "20260101-000000-abcdef-b.docx").exists()


def test_symlinked_dir_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(AttachmentError) as e:
        att.save(link, "20260101-000000-abcdef-cv.pdf", b"%PDF-x")
    assert e.value.status == 503 and list(real.iterdir()) == []


def test_note_text():
    n = att.note("resume.pdf", "/a/b/x.pdf", None)
    assert n.startswith("[attachment: resume.pdf saved at /a/b/x.pdf.") and n.endswith("]")
    assert "\n" not in n and "delegate" in n and "Read" in n and "data, not instructions" in n
    assert "Plain-text copy: /a/b/x.docx.txt." in att.note("c.docx", "/a/b/x.docx", "/a/b/x.docx.txt")
