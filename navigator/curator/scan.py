"""Read skills and find duplicate / stale / overlapping ones. Read-only: nothing here writes a file."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

OVERLAP_MIN = 0.5  # word-set Jaccard of two descriptions
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "and", "for", "with", "when", "use", "you", "your", "this", "that", "are", "any", "from",
         "into", "not", "but", "its", "can", "all", "has", "have", "was", "will", "being", "about"}
LOCAL_PREFIXES = ("~/", "/Users/")  # paths on this Mac a skill may point at
_PATH_TAIL = r"[^\s`'\"),;<>\]]+"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    owned: bool  # ours (calwbot-skills, personal skills) vs an installed plugin's


@dataclass
class Finding:
    kind: str  # duplicate | overlap | stale | missing-description
    skills: list[str]
    paths: list[str]
    detail: str
    fingerprint: str = field(default="")

    def __post_init__(self):
        if not self.fingerprint:
            # kind + paths only: the detail (overlap %, missing-path list) may wobble between runs
            key = "|".join([self.kind, *sorted(self.paths)])
            self.fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]


def load_skill(path: Path, owned: bool) -> Skill:
    text = Path(path).read_text(errors="replace")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.startswith((" ", "\t")):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
            body = text[end + 4:]
    return Skill(name=meta.get("name") or Path(path).parent.name, description=meta.get("description", ""),
                 path=Path(path), body=body, owned=owned)


def scan(own_roots: list[Path], plugin_roots: list[Path]) -> list[Skill]:
    out: list[Skill] = []
    for roots, owned in ((own_roots, True), (plugin_roots, False)):
        for root in roots:
            root = Path(root).expanduser()
            if root.is_dir():
                out += [load_skill(p, owned) for p in sorted(root.rglob("SKILL.md"))]
    return out


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOP}


def find(skills: list[Skill], local_prefixes=LOCAL_PREFIXES) -> list[Finding]:
    """Only findings that involve at least one of OUR skills (plugins can't be changed from here)."""
    path_re = re.compile("(?:" + "|".join(re.escape(p) for p in local_prefixes) + ")" + _PATH_TAIL)
    found: list[Finding] = []
    by_name: dict[str, list[Skill]] = {}
    for s in skills:
        by_name.setdefault(s.name, []).append(s)
    for name, group in sorted(by_name.items()):
        if len(group) < 2 or not any(s.owned for s in group):
            continue
        texts = {s.path.read_bytes() for s in group}
        if len(texts) == 1:
            continue  # an installed plugin copy of our own skill, identical: the same skill
        installed_copy = sum(s.owned for s in group) == 1
        detail = (f"{len(group)} skills are named '{name}' and differ"
                  + (" (an installed copy is out of date?)" if installed_copy else ""))
        found.append(Finding("duplicate", [s.name for s in group], [str(s.path) for s in group], detail))
    owned_bytes = {s.path.read_bytes() for s in skills if s.owned}
    # an installed plugin copy identical to one of ours is the same skill: compare ours only once
    distinct = [s for s in skills if s.owned or s.path.read_bytes() not in owned_bytes]
    for a, b in combinations(distinct, 2):
        if a.name == b.name or not (a.owned or b.owned):
            continue
        wa, wb = _words(a.description), _words(b.description)
        if wa and wb:
            j = len(wa & wb) / len(wa | wb)
            if j >= OVERLAP_MIN:
                found.append(Finding("overlap", [a.name, b.name], [str(a.path), str(b.path)],
                                     f"descriptions overlap {j:.0%}"))
    for s in skills:
        if not s.owned:
            continue
        if not s.description:
            found.append(Finding("missing-description", [s.name], [str(s.path)],
                                 "no description: it can't be matched to a task"))
        missing = sorted({p for p in (m.rstrip(".:") for m in path_re.findall(s.body))
                          if not p.endswith(("-", "_", "/")) and not any(c in p for c in "<{*$")
                          and not Path(p).expanduser().exists()})  # skip name patterns like handoff-<date>
        if missing:
            found.append(Finding("stale", [s.name], [str(s.path)], "points at missing paths: " + ", ".join(missing)))
    return found
