"""Proposal triage (operator 2026-10-03: 'show me 3 things at a time that take priority').

Each cycle, before planning:
  1. retire: an open proposal whose milestone code (G1, N5b, C4b, ...) or title already
     belongs to approved work, or that repeats a newer open proposal, is marked
     superseded - the operator never sees it.
  2. rank: the rest are ordered by the planner-role model, which sees the operator's
     priorities, the operator's own past approve/decline decisions (so ranking follows
     what the operator actually picks) and recent run outcomes. The model may also
     drop low-value proposals; those are declined. Without a model, or when it fails,
     a deterministic order is used (milestone proposals first, then newest).
The order is persisted (triage.json) so Telegram 'status' shows the top 3 without a
model call; a new top 3 is pushed to the operator once. Every numbered list sent to the
operator (that push, 'status', 'more', 'all') is stored as 'shown' (N13): the numbers
the operator types resolve against that snapshot, never against the live order at the
moment the reply arrives.

Every write to triage.json goes through update(): a blocking lock beside the file
(file_lock) serializes the daemon and the Telegram reader, the file is replaced
atomically, and the 'more' cursor never moves backwards unless a writer asks for a
rewind explicitly (set_cursor: the operator restarting paging).
"""
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from navigator.contracts.enums import ProposalStatus
from navigator.contracts.event import Event
from navigator.observability.logging import get_logger
from navigator.runtime.lock import file_lock

log = get_logger("navigator.triage")

DEFAULT_TRIAGE_PATH = "/Users/Manoj/calwbot/logs/navigator/triage.json"
TOP_N = 3
RERANK_AFTER_S = 24 * 3600
DUPLICATE_THRESHOLD = 0.5
MAX_DROPS_PER_PASS = 10
# The pair the conductor files builds with (a test pins these equal to runtime.loop's; no import: it pulls
# in the executor stack). A build the operator asked for in chat is never dropped by the ranking model.
OPERATOR_ORIGIN = "operator"
DRAFT_PREFIX = "[draft]"


def is_conductor_build(p) -> bool:
    return p.origin == OPERATOR_ORIGIN and (p.title or "").strip().lower().startswith(DRAFT_PREFIX)

DEFAULT_BRIEF = ("Navigator is the #1 priority: work that makes navigator more autonomous, "
                 "reliable and self-improving ranks highest. Operational edge beats new "
                 "strategy combos. Trader is benched. Test-only proposals for side projects "
                 "rank low unless they close a real safety gap.")

# milestone codes in titles: 'N5b: ...', '... (C4b)', 'S3a', 'G1e-1d'
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]\d{1,2}[a-z]?(?:-\d+[a-z]?)?)(?![A-Za-z0-9])")
_STOPWORDS = frozenset("a an the and or for to of in on with add so that every all across "
                       "tests test regression using real apis no skips".split())
_WORD = re.compile(r"[a-z0-9]+")


def codes(title: str) -> set[str]:
    return set(_CODE.findall(title or ""))


_CODE_PARTS = re.compile(r"([A-Z]\d{1,2})([a-z]?)(.*)")


def covered_by(code: str, done_codes: set[str]) -> str | None:
    """The done code that covers `code`: the same code, or a later revision of the same
    milestone ('C4c' covers 'C4b', 'G1c' covers 'G1'). 'N10' never covers 'N1'."""
    if code in done_codes:
        return code
    m = _CODE_PARTS.fullmatch(code)
    if not m or m[3]:
        return None
    for d in sorted(done_codes):
        dm = _CODE_PARTS.fullmatch(d)
        if dm and dm[1] == m[1] and dm[2] > m[2]:
            return d
    return None


def _words(title: str) -> set[str]:
    return {w for w in _WORD.findall((title or "").lower()) if w not in _STOPWORDS}


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= DUPLICATE_THRESHOLD


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def retire_candidates(open_props: list, done_props: list) -> list[tuple]:
    """(proposal, reason) for open proposals that repeat done work or a newer open one."""
    done_codes: set[str] = set()
    for d in done_props:
        done_codes |= codes(d.title)
    out, kept = [], []
    for p in sorted(open_props, key=lambda p: _aware(p.created_at), reverse=True):
        cover = next((c for c in map(lambda k: covered_by(k, done_codes), sorted(codes(p.title)))
                      if c), None)
        if cover:
            out.append((p, f"{cover} already done"))
            continue
        twin = next((d for d in done_props if d.repo == p.repo and similar(p.title, d.title)),
                    None)
        if twin is not None:
            out.append((p, f"repeats done work: {twin.title[:60]}"))
            continue
        newer = next((k for k in kept if k.repo == p.repo and similar(p.title, k.title)), None)
        if newer is not None:
            out.append((p, f"repeats newer proposal: {newer.title[:60]}"))
            continue
        kept.append(p)
    return out


def fallback_order(props: list) -> list:
    """Milestone proposals first (they come from the operator's roadmap), then newest."""
    return sorted(props, key=lambda p: (not codes(p.title), -_aware(p.created_at).timestamp()))


def fingerprint(props: list) -> str:
    return hashlib.sha256("\n".join(sorted(p.id for p in props)).encode()).hexdigest()[:16]


def load(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".triage.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lock_path(path) -> Path:
    """The advisory lock beside triage.json; every writer (daemon and reader) takes it."""
    path = Path(path)
    return path.with_name(path.name + ".lock")


def cursor_of(state: dict) -> int:
    try:
        return max(0, int(state.get("cursor") or 0))
    except (TypeError, ValueError):
        return 0


def update(path, mutate: Callable[[dict], dict], *, rewind_cursor: bool = False) -> dict:
    """Locked, atomic read-modify-write of triage.json. `mutate` gets the state as it is
    on disk NOW (never a stale copy) and returns the new state. The cursor never moves
    backwards unless rewind_cursor is set: a writer that lost it (an old load, a rebuilt
    dict) or that carries a lower value keeps what another process stored meanwhile."""
    with file_lock(lock_path(path)):
        state = load(path)
        before = cursor_of(state)
        new = mutate(dict(state))
        if not rewind_cursor and cursor_of(new) < before:
            new["cursor"] = before
        _save(path, new)
        return new


def ordered(open_props: list, path) -> list[tuple]:
    """(proposal, why) for every open proposal in triage order: ranked ids first, then
    anything newer than the last ranking in fallback order."""
    ranked = load(path).get("ranked") or []
    by_id = {p.id: p for p in open_props}
    out, seen = [], set()
    for r in ranked:
        if isinstance(r, dict) and r.get("id") in by_id and r["id"] not in seen:
            out.append((by_id[r["id"]], str(r.get("why") or "")))
            seen.add(r["id"])
    out += [(p, "") for p in fallback_order([p for p in open_props if p.id not in seen])]
    return out


_RANK_SPEC = ('Respond with ONLY a JSON object: {"ranked": [{"id": "<proposal id>", '
              '"why": "<one short sentence: why now>"}], "drop": [{"id": "<proposal id>", '
              '"why": "<why it is not worth the operator\'s time>"}]}. "ranked" lists the '
              'proposals worth doing, most important first. Put a proposal in "drop" only '
              'when it is clearly low value, already covered, or contradicts the priorities.')


class Triage:
    def __init__(self, act, *, query_fn: Callable[[str], str] | None, notifier,
                 path=DEFAULT_TRIAGE_PATH, brief: str = DEFAULT_BRIEF,
                 outcomes: Callable[[], list] = lambda: [],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.act = act; self.query_fn = query_fn; self.notifier = notifier
        self.path = path; self.brief = brief; self.outcomes = outcomes; self.now = now

    def _event(self, p, kind: str, why: str) -> None:
        try:
            self.act.emit_event(Event(kind=kind, correlation_id=p.id,
                                      payload={"proposal_id": p.id, "title": p.title,
                                               "reason": why, "actor": "navigator-triage"}))
        except Exception as e:
            log.warning("triage.event_failed", error=type(e).__name__)

    def _prompt(self, props: list) -> str:
        decided = sorted(self.act.list_proposals(ProposalStatus.approved)
                         + self.act.list_proposals(ProposalStatus.declined),
                         key=lambda p: _aware(p.created_at), reverse=True)[:40]
        outcomes = [{"status": getattr(o, "status", ""), "summary": str(getattr(o, "summary", ""))[:160]}
                    for o in self.outcomes()[:10]]
        ctx = {
            "operator_priorities": self.brief,
            "operator_past_decisions": [{"title": p.title[:120], "decision": p.status.value}
                                        for p in decided],
            "recent_run_outcomes": outcomes,
            "open_proposals": [{"id": p.id, "repo": Path(p.repo).name, "title": p.title[:160],
                                "rationale": (p.rationale or "")[:300]} for p in props],
        }
        return ("role: triage\nYou rank an autonomous coding agent's proposals so a busy "
                "operator only reviews the 3 that matter most. Learn from the operator's "
                "past decisions: rank like what they approved, drop like what they declined.\n"
                f"context: {json.dumps(ctx)}\n{_RANK_SPEC}")

    def _rank(self, props: list) -> tuple[list[tuple], list[tuple], str]:
        if self.query_fn is not None:
            try:
                raw = self.query_fn(self._prompt(props)).strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
                data = json.loads(raw)
                by_id = {p.id: p for p in props}
                ranked = [(by_id[r["id"]], str(r.get("why", ""))[:200])
                          for r in data.get("ranked", []) if r.get("id") in by_id]
                seen = {p.id for p, _ in ranked}
                drop = [(by_id[d["id"]], str(d.get("why", ""))[:200])
                        for d in data.get("drop", [])
                        if d.get("id") in by_id and d["id"] not in seen
                        and not is_conductor_build(by_id[d["id"]])][:MAX_DROPS_PER_PASS]
                dropped = {p.id for p, _ in drop}
                rest = [p for p in props if p.id not in seen and p.id not in dropped]
                return ranked + [(p, "") for p in fallback_order(rest)], drop, "model"
            except Exception as e:
                log.warning("triage.rank_failed", error=type(e).__name__)
        return [(p, "") for p in fallback_order(props)], [], "fallback"

    def run(self) -> dict:
        open_props = self.act.list_proposals(ProposalStatus.proposed)
        done = self.act.list_proposals(ProposalStatus.approved)
        retired = retire_candidates(open_props, done)
        for p, why in retired:
            self.act.set_proposal_status(p.id, ProposalStatus.superseded)
            self._event(p, "proposal.triage_superseded", why)
        gone = {p.id for p, _ in retired}
        live = [p for p in open_props if p.id not in gone]

        state = load(self.path)
        fp = fingerprint(live)
        age = self.now().timestamp() - float(state.get("ts_epoch") or 0)
        dropped: list[tuple] = []
        if state.get("fingerprint") == fp and age < RERANK_AFTER_S:
            order = ordered(live, self.path)
            source = state.get("source", "cached")
        else:
            order, dropped, source = self._rank(live)
            for p, why in dropped:
                self.act.set_proposal_status(p.id, ProposalStatus.declined)
                self._event(p, "proposal.triage_declined", why)
            kept = [p for p, _ in order]
            ranking = {"ts": self.now().isoformat(timespec="seconds"),
                       "ts_epoch": self.now().timestamp(), "fingerprint": fingerprint(kept),
                       "source": source,
                       "ranked": [{"id": p.id, "title": p.title, "why": why}
                                  for p, why in order]}
            # locked merge: the reader may have moved the cursor while the model ran, so
            # cursor and top_sent come from the file as it is now, not from the old load
            state = update(self.path, lambda s: {**s, **ranking})

        top = [p.id for p, _ in order[:TOP_N]]
        if retired or dropped:
            lines = [f"Navigator triage: retired {len(retired) + len(dropped)} proposal(s)"]
            lines += [f"- {p.title[:90]} ({why})" for p, why in (retired + dropped)[:15]]
            if len(retired) + len(dropped) > 15:
                lines.append(f"... and {len(retired) + len(dropped) - 15} more")
            self.notifier.send("\n".join(lines))
        if top and top != state.get("top_sent"):
            # the snapshot is on disk before the message goes out, so a reply to it can
            # never resolve against the previous list; the cursor is never written here
            shown = snapshot(order)
            state = update(self.path, lambda s: {**s, "top_sent": top, "shown": shown})
            self.notifier.send(format_top(order, start=0))
        return {"triage_retired": len(retired), "triage_dropped": len(dropped),
                "triage_open": len(order), "triage_source": source}


def format_top(order: list[tuple], start: int = 0, n: int = TOP_N) -> str:
    chunk = order[start:start + n]
    if not chunk:
        return ("Navigator: nothing waiting for a decision" if not order
                else "Navigator: no more proposals - send 'status' for the top 3")
    lines = [f"Navigator: top {start + 1}-{start + len(chunk)} of {len(order)} to decide"]
    for i, (p, why) in enumerate(chunk, start + 1):
        lines.append(f"{i}. [{Path(p.repo).name}] {p.title}")
        if why:
            lines.append(f"   why: {why}")
    lines.append(f"reply: approve {start + 1} / decline {start + 1}"
                 + (" / more" if start + n < len(order) else ""))
    return "\n".join(lines)


def set_cursor(path, start: int) -> None:
    """Where 'more' continues from; kept beside the ranking for the bot process.
    Serialized with every other writer and written atomically. An explicit set is the
    operator's own position ('status' restarts paging at the top), so it may rewind;
    use advance_cursor for writes that must only ever move forward."""
    start = max(0, int(start))
    update(path, lambda s: {**s, "cursor": start}, rewind_cursor=True)


def advance_cursor(path, start: int) -> int:
    """Move the cursor forward to `start`, never backwards; returns the stored cursor.
    Interleaved advances from two processes end at the highest position."""
    start = max(0, int(start))
    return cursor_of(update(path, lambda s: {**s, "cursor": start}))


# ---- the list the operator last saw: what typed numbers refer to (N13) --------------

def snapshot(order: list[tuple], now: float | None = None) -> dict:
    """The full numbered order as it was sent to the operator: entry k (0-based) is the
    number k+1 they see. Stored as triage.json 'shown'; ts_epoch identifies the list so
    the 'more' cursor is only ever applied to the snapshot it was taken on."""
    return {"ts_epoch": time.time() if now is None else float(now),
            "entries": [{"id": p.id, "title": p.title, "repo": Path(p.repo).name,
                         "why": why} for p, why in order]}


def set_shown(path, order: list[tuple], cursor: int = 0) -> dict:
    """Store `order` as the list the operator is looking at and restart paging there:
    one locked write of the snapshot, the cursor and the snapshot the cursor belongs to.
    Returns the new state."""
    shown = snapshot(order)
    cursor = max(0, int(cursor))
    return update(path, lambda s: {**s, "shown": shown, "cursor": cursor,
                                   "cursor_for": shown["ts_epoch"]}, rewind_cursor=True)


def shown_entries(state: dict) -> list[dict]:
    """The entries of the stored snapshot, or [] when there is none or it is malformed."""
    shown = state.get("shown") if isinstance(state, dict) else None
    entries = shown.get("entries") if isinstance(shown, dict) else None
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries):
        return []
    return entries


def format_shown(entries: list[dict], open_ids: set[str], start: int = 0,
                 n: int = TOP_N) -> str:
    """format_top rendered from a snapshot; an entry decided since it was taken (its id
    no longer among open_ids) is marked so the operator does not pick its number."""
    chunk = entries[start:start + n]
    if not chunk:
        return ("Navigator: nothing waiting for a decision" if not entries
                else "Navigator: no more proposals - send 'status' for the top 3")
    lines = [f"Navigator: top {start + 1}-{start + len(chunk)} of {len(entries)} to decide"]
    for i, e in enumerate(chunk, start + 1):
        line = f"{i}. [{e.get('repo') or ''}] {e.get('title') or ''}"
        lines.append(line if e["id"] in open_ids else f"{line} (decided)")
        if e.get("why"):
            lines.append(f"   why: {e['why']}")
    lines.append(f"reply: approve {start + 1} / decline {start + 1}"
                 + (" / more" if start + n < len(entries) else ""))
    return "\n".join(lines)
