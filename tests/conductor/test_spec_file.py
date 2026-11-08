"""delegate spec_path (2026-11-07): the conductor reads an allowlisted spec file and files its text."""
import asyncio
import os
import signal

import pytest

from navigator.clients.act_client import FakeACTClient
from navigator.conductor import builds as B
from navigator.conductor import tools


class Cfg:
    tier_c_paths = ()
    tier_floor = "A"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = os.path.join(os.path.realpath(tmp_path), "scratch")
    os.makedirs(r)
    monkeypatch.setattr(B, "spec_roots", lambda: [r])
    monkeypatch.setattr(B, "repo_config_for", lambda s, p: Cfg() if s is not None else None)
    return r


def mk(tmp_path):
    act = FakeACTClient()
    b = B.Builds(tmp_path / "builds.json", act, settings=object(), on_finish=lambda r: None,
                 base_sha_fn=lambda repo: "abc123")
    return b, act


def write(root, name="x.md", text=None, data=None):
    p = os.path.join(root, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if data is not None else (text or "spec line here. " * 200).encode())
    return p


def test_spec_file_becomes_intended_changes(tmp_path, root):
    text = ("word " * 600)[:3000]
    p = write(root, text=text)
    b, act = mk(tmp_path)
    rec = b.file("navigator", "T", "summary summary summary summary", ["docs/a.md"], "web", spec_path=p)
    prop = act.proposals[rec["proposal_id"]]
    assert prop.intended_changes == text and "summary summary" in prop.rationale
    assert rec["spec_chars"] == len(text)


def _refusals(root, tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    secret = out / "s.md"
    secret.write_text("secret " * 100)
    write(root, "ok.md")
    os.symlink(secret, os.path.join(root, "link.md"))
    os.symlink(out, os.path.join(root, "linkdir"))
    os.link(secret, os.path.join(root, "hard.md"))
    fifo = os.path.join(root, "f.md")
    os.mkfifo(fifo)
    os.makedirs(os.path.join(root, "d.md"))
    return [
        "rel.md", root + "/../etc/x.md", root + "/a/../b.md", "/etc/hosts", root + "-evil/x.md",
        os.path.join(root, "link.md"), os.path.join(root, "linkdir", "s.md"),
        write(root, ".env.md"), write(root, ".git/x.md"), write(root, "Tra" + "der/x.md"),
        write(root, "tra" + "der/y.md"), write(root, "lo" + "gs/x.md"), os.path.join(root, "hard.md"),
        root.upper() + "/ok.md", write(root, "x.py"), os.path.join(root, "d.md"), fifo,
        write(root, "big.md", data=b"a" * 100_001), write(root, "bad.md", data=b"\xff\xfe" * 300),
        write(root, "nul.md", data=b"a" * 500 + b"\0"), write(root, "short.md", text="tiny"),
        "", None, 5,
    ]


def test_spec_path_refusals(tmp_path, root):
    b, act = mk(tmp_path)
    for p in _refusals(root, tmp_path):
        signal.alarm(5)
        try:
            with pytest.raises(B.BuildRefused):
                b.file("navigator", "T", "summary summary summary summary", ["docs/a.md"], "web", spec_path=p)
        finally:
            signal.alarm(0)
        assert len(act.proposals) == 0, p


def test_cap_boundary(root):
    text, n = B.read_spec(write(root, "ok.md", data=b"a " * (B.SPEC_MAX_BYTES // 2)))
    assert len(text) == B.SPEC_MAX_BYTES
    with pytest.raises(B.BuildRefused):
        B.read_spec(write(root, "big.md", data=b"a " * (B.SPEC_MAX_BYTES // 2) + b"a"))


def test_spec_masks_tokens_keeps_paths(root):
    sha = "c4576c4f0f3a9d0f1d2b3c4d5e6f708192a3b4c5"
    body = ("see sk-abcdefghijkl and ghp_abcdefghijkl and 123456789:" + "A" * 28 + " and AKIAABCDEFGHIJKLMNOP "
            f"path navigator/tests/conductor/test_stage3_cards sha {sha} " + "filler words here " * 40)
    p = write(root, text=body)
    text, n = B.read_spec(p)
    assert n == 4 and "navigator/tests/conductor/test_stage3_cards" in text and sha in text
    assert "sk-abc" not in text and "AKIA" not in text and open(p).read() == body


def test_spec_path_does_not_change_tier(tmp_path, root):
    p = write(root)
    b, act = mk(tmp_path)
    calls = []
    b._notify_needs = calls.append
    d = b.file("navigator", "T", "summary summary summary summary", ["docs/a.md"], "web", spec_path=p)
    assert d["tier1"] is True
    c = b.file("navigator", "T2", "summary summary summary summary", ["navigator/conductor/x.py"], "web", spec_path=p)
    assert c["tier1"] is False and c["approval_reason"]
    e = b.file("navigator", "T3", "summary summary summary summary", [], "web", spec_path=p)
    assert e["tier1"] is False and len(calls) == 2


def test_race_symlink_swap_refused(root, tmp_path, monkeypatch):
    p = write(root)
    real = os.path.realpath
    target = tmp_path / "t.md"
    target.write_text("z" * 500)

    def swap(path):
        out = real(path)
        os.remove(p)
        os.symlink(target, p)
        return out
    monkeypatch.setattr(os.path, "realpath", swap)
    with pytest.raises(B.BuildRefused):
        B.read_spec(p)


def test_roots_come_from_build_repos():
    roots = B.spec_roots()
    assert "/Users/Manoj/calwbot/tmp_claude" in roots and "/Users/Manoj/calwbot/docs" in roots
    assert not any("tra" + "der" in r for r in roots)
    assert any(r.endswith("/navigator/docs") for r in roots) or "navigator" not in B.BUILD_REPOS


def test_act_accepts_100kb_proposal():
    from navigator.contracts.proposal import Proposal
    act = FakeACTClient()
    big = "a" * 100_000
    p = Proposal(origin="x", proposal_origin_hash="h", title="t", rationale="r", repo="/r",
                 base_commit_sha="abc", intended_changes=big, affected_files=[])
    assert act.create_proposal(p).intended_changes == big


# ---- delegate tool wiring ----

class FakeBuilds:
    current_via = None

    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def file(self, project, title, task, files, via, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"proposal_id": "p1", "title": title, "tier1": False, "approval_reason": "x", "spec_chars": 1234 if kw else None}


def test_delegate_build_passes_spec_path_only_when_given():
    base = {"kind": "build", "project": "navigator", "title": "t", "task": "x" * 450}
    fb = FakeBuilds()
    r = asyncio.run(tools.delegate_text(None, base, fb))
    assert fb.calls == [{}] and "spec read from file" not in r
    r = asyncio.run(tools.delegate_text(None, {**base, "spec_path": "/a/b.md"}, fb))
    assert fb.calls[1] == {"spec_path": "/a/b.md"} and "spec read from file (1234 chars)" in r and "/a/b.md" not in r
    r = asyncio.run(tools.delegate_text(None, {**base, "spec_path": "/a/b.md"}, FakeBuilds(B.BuildRefused("nope"))))
    assert r == "refused: nope"


def test_delegate_build_refuses_timeout_s_and_quick_refuses_spec_path():
    base = {"kind": "build", "project": "navigator", "title": "t", "task": "x" * 450}
    assert asyncio.run(tools.delegate_text(None, {**base, "timeout_s": 100}, FakeBuilds())).startswith("refused")
    q = {"kind": "quick", "title": "t", "task": "x" * 450, "spec_path": "/a.md"}
    assert asyncio.run(tools.delegate_text(None, q)).startswith("refused")


def test_delegate_schema_has_new_fields():
    props = tools.DELEGATE_SCHEMA["properties"]
    assert "spec_path" in props and "timeout_s" in props
