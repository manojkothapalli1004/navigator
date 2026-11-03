"""Curator (design spec §11, built 2026-11-02): scan skills, find duplicate / stale / overlapping ones,
emit one review-gated consolidation proposal. It never edits a skill."""
import hashlib
import json
from pathlib import Path

from navigator.curator import scan as cscan
from navigator.curator.__main__ import main


def skill(root: Path, folder: str, name: str, desc: str | None, body: str = "") -> Path:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\n" + (f"description: {desc}\n" if desc is not None else "") + "---\n"
    (d / "SKILL.md").write_text(fm + body)
    return d / "SKILL.md"


def _roots(tmp_path):
    own, plug = tmp_path / "own", tmp_path / "plugins"
    skill(own, "a", "cold-audit", "Use before recommending a merge to audit a change with a cold reviewer")
    skill(own, "b", "audit-cold", "Use before recommending a merge to audit a change with a fresh cold reviewer")
    skill(own, "c", "relay", "Telegram relay helper", body=f"Run {tmp_path}/gone/relay.sh first\n")
    skill(own, "d", "nodesc", None)
    skill(plug, "x", "cold-audit", "Plugin version of the same name")
    skill(plug, "y", "pdf", "Read and write PDF files")
    skill(plug, "z", "pdf-tools", "Read and write PDF files quickly")  # plugin-only overlap: not ours
    return own, plug


def test_parse_frontmatter(tmp_path):
    p = skill(tmp_path, "s", "my-skill", "Does a thing: well")
    s = cscan.load_skill(p, owned=True)
    assert s.name == "my-skill" and s.description == "Does a thing: well" and s.owned


def test_findings(tmp_path):
    own, plug = _roots(tmp_path)
    found = cscan.find(cscan.scan([own], [plug]), (str(tmp_path) + '/',))
    kinds = {(f.kind, tuple(sorted(f.skills))) for f in found}
    assert ("duplicate", ("cold-audit", "cold-audit")) in kinds
    assert ("overlap", ("audit-cold", "cold-audit")) in kinds
    assert ("missing-description", ("nodesc",)) in kinds
    stale = [f for f in found if f.kind == "stale"]
    assert stale and stale[0].skills == ["relay"] and f"{tmp_path}/gone/relay.sh" in stale[0].detail
    # findings that only involve plugin skills can't be acted on: dropped
    assert not any(set(f.skills) <= {"pdf", "pdf-tools"} for f in found)


def test_existing_paths_are_not_stale(tmp_path):
    own = tmp_path / "own"; (tmp_path / "here.sh").write_text("")
    skill(own, "c", "relay", "x", body=f"Run {tmp_path}/here.sh\n")
    assert not [f for f in cscan.find(cscan.scan([own], []), (str(tmp_path) + '/',)) if f.kind == "stale"]


def test_fingerprints_stable_and_distinct(tmp_path):
    own, plug = _roots(tmp_path)
    a = cscan.find(cscan.scan([own], [plug])); b = cscan.find(cscan.scan([own], [plug]))
    assert [f.fingerprint for f in a] == [f.fingerprint for f in b]
    assert len({f.fingerprint for f in a}) == len(a)


class Notes:
    def __init__(self):
        self.sent = []
    def send(self, t):
        self.sent.append(t)


class ACT:
    def __init__(self):
        self.created = []
    def create_proposal(self, p):
        self.created.append(p); return p


def _hashes(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(tmp_path, *args, act=None, notes=None):
    own, plug = tmp_path / "own", tmp_path / "plugins"
    notes = notes or Notes(); act = act or ACT()
    rc = main(["run", *args], own_roots=[own], plugin_roots=[plug], root=tmp_path / "out", act=act,
              notifier=notes, repo_root=tmp_path, base_sha=lambda: "abc123",
              local_prefixes=(str(tmp_path) + '/',))
    return rc, notes, act


def test_report_only_by_default_and_never_edits_skills(tmp_path):
    _roots(tmp_path)
    before = _hashes(tmp_path / "own") | _hashes(tmp_path / "plugins")
    rc, notes, act = _run(tmp_path)
    assert rc == 1 and act.created == []  # findings, nothing filed without --file
    assert len(notes.sent) == 1 and "Curator: 4 findings" in notes.sent[0]
    assert (_hashes(tmp_path / "own") | _hashes(tmp_path / "plugins")) == before
    report = json.loads(next((tmp_path / "out").glob("*/report.json")).read_text())
    assert len(report["findings"]) == 4


def test_quiet_when_nothing_changed(tmp_path):
    _roots(tmp_path)
    _run(tmp_path)
    rc, notes, _ = _run(tmp_path)
    assert rc == 1 and notes.sent == []


def test_file_emits_one_review_gated_proposal_once(tmp_path):
    _roots(tmp_path)
    rc, notes, act = _run(tmp_path, "--file")
    assert len(act.created) == 1
    p = act.created[0]
    assert p.origin == "curator" and p.repo == str(tmp_path) and p.base_commit_sha == "abc123"
    assert p.status.value == "proposed"  # review-gated: a human approves before anything is built
    assert "never edits" in p.intended_changes.lower() or "consolidat" in p.intended_changes.lower()
    assert all(not f.startswith("/") for f in p.affected_files)  # repo-relative, our skills only
    _, _, act2 = _run(tmp_path, "--file")
    assert act2.created == []  # same findings: not filed again


def test_clean_run(tmp_path):
    skill(tmp_path / "own", "a", "only-one", "A unique skill about weather")
    rc, notes, act = _run(tmp_path, "--file")
    assert rc == 0 and notes.sent == [] and act.created == []


def test_installed_copy_of_our_own_skill_is_not_a_duplicate(tmp_path):
    own, plug = tmp_path / "own", tmp_path / "plugins"
    a = skill(own, "s", "handoffs", "Write a session handoff", body="same\n")
    b = skill(plug, "calwbot-skills/s", "handoffs", "Write a session handoff", body="same\n")
    assert not [f for f in cscan.find(cscan.scan([own], [plug])) if f.kind == "duplicate"]
    b.write_text(b.read_text() + "old copy\n")  # the installed copy differs: worth flagging
    d = [f for f in cscan.find(cscan.scan([own], [plug])) if f.kind == "duplicate"]
    assert d and "differ" in d[0].detail


def test_path_prefix_patterns_are_not_stale(tmp_path):
    own = tmp_path / "own"
    skill(own, "c", "h", "x", body=f"Write {tmp_path}/logs/handoff-<date>.md or {tmp_path}/logs/handoff- files\n")
    assert not [f for f in cscan.find(cscan.scan([own], []), (str(tmp_path) + '/',)) if f.kind == "stale"]


# ---- cold-audit fixes ----

class EventACT(ACT):
    def __init__(self):
        super().__init__(); self.events = []
    def emit_event(self, e):
        self.events.append(e)


def test_findings_event_emitted_on_change(tmp_path):
    _roots(tmp_path)
    act = EventACT(); _run(tmp_path, act=act)
    assert len(act.events) == 1 and act.events[0].kind == "curator.findings"
    assert act.events[0].payload["count"] == 4
    act2 = EventACT(); _run(tmp_path, act=act2)
    assert act2.events == []  # nothing changed


def test_fingerprint_ignores_volatile_detail(tmp_path):
    a = cscan.Finding("overlap", ["x", "y"], ["/a", "/b"], "descriptions overlap 55%")
    b = cscan.Finding("overlap", ["x", "y"], ["/a", "/b"], "descriptions overlap 61%")
    assert a.fingerprint == b.fingerprint


def test_a_b_a_does_not_refile(tmp_path):
    own, plug = _roots(tmp_path)
    _run(tmp_path, "--file")
    extra = skill(own, "e", "extra", None)
    _, _, act_b = _run(tmp_path, "--file")
    assert len(act_b.created) == 1
    extra.unlink(); extra.parent.rmdir()
    _, _, act_a = _run(tmp_path, "--file")
    assert act_a.created == []  # back to the first set: already filed once


def test_repo_is_absolute_and_nothing_filed_without_affected_files(tmp_path):
    _roots(tmp_path)
    _, _, act = _run(tmp_path, "--file")
    assert act.created[0].repo == str(tmp_path)  # Navigator keys repos by absolute path
    other = tmp_path / "elsewhere"; skill(other, "q", "lonely", None)
    notes, act2 = Notes(), ACT()
    main(["run", "--file"], own_roots=[other], plugin_roots=[], root=tmp_path / "out2", act=act2, notifier=notes,
         repo_root=tmp_path / "own", base_sha=lambda: "abc", local_prefixes=(str(tmp_path) + "/",))
    assert act2.created == []  # its only finding is outside the skills repo: nothing to build there


def test_no_base_sha_no_filing(tmp_path):
    _roots(tmp_path)
    notes, act = Notes(), ACT()
    main(["run", "--file"], own_roots=[tmp_path / "own"], plugin_roots=[tmp_path / "plugins"], root=tmp_path / "out",
         act=act, notifier=notes, repo_root=tmp_path, base_sha=lambda: "", local_prefixes=(str(tmp_path) + "/",))
    assert act.created == [] and any("not filed" in s for s in notes.sent)


def test_write_errors_exit_2_with_one_line(tmp_path, monkeypatch):
    _roots(tmp_path)
    (tmp_path / "out").mkdir(); (tmp_path / "out" / "state.json").write_text("[]")  # parses, not a dict
    rc, notes, _ = _run(tmp_path)
    assert rc == 1  # a non-dict state is just ignored
    def boom(*a, **k):
        raise PermissionError("ro")
    monkeypatch.setattr(Path, "write_text", boom)
    rc, notes, _ = _run(tmp_path)
    assert rc == 2 and len(notes.sent) == 1 and "Curator: run failed" in notes.sent[0]


def test_identical_installed_copy_not_double_counted_in_overlap(tmp_path):
    own, plug = tmp_path / "own", tmp_path / "plugins"
    skill(own, "a", "alpha", "Audit a change with a cold reviewer before a merge")
    skill(own, "b", "beta", "Audit a change with a fresh cold reviewer before a merge")
    skill(plug, "pkg/a", "alpha", "Audit a change with a cold reviewer before a merge")  # identical copy
    overlaps = [f for f in cscan.find(cscan.scan([own], [plug])) if f.kind == "overlap"]
    assert len(overlaps) == 1


def test_empty_roots_are_not_replaced_by_the_live_ones(tmp_path, monkeypatch):
    # terminal review 1: [] must mean "no roots", never "fall back to the real skill folders"
    from navigator.curator import __main__ as cm
    seen = []
    monkeypatch.setattr(cm.cscan, "scan", lambda own, plug: seen.append((list(own), list(plug))) or [])
    main(["run"], own_roots=[tmp_path / "x"], plugin_roots=[], root=tmp_path / "o", act=ACT(), notifier=Notes(),
         repo_root=tmp_path, base_sha=lambda: "a")
    assert seen == [([tmp_path / "x"], [])]
