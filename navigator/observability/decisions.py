"""Decisions inbox (operator 2026-10-24): one list of everything waiting on the operator.

2026-10-23 an audited merge (Quota) sat unmerged overnight: the operator is told once,
when a run becomes merge-ready, and never again, and the manual steps only the operator
can do (plist installs, the charger) lived only in terminal chat. This module keeps one
list of three kinds of item:
  merge     audited runs at the merge gate (telegram_inbox._candidates) - reply 'merge <name>'
  manual    steps tracked in a small JSON file (add/done via this module's CLI:
            `python -m navigator.observability.decisions add <id> <title> <how>`;
            with `--script <path>` the step carries ONE pinned script the operator
            runs from Telegram with 'run <id>' - see actions.py)
  proposal  proposals waiting for approve/decline
and three ways to see it:
  'decisions' in Telegram (telegram_proposals.handle_operator_message): the list, read-only
  a nudge once a merge/manual item has waited NUDGE_AFTER_S (at most once per item per
    local day; proposals never nudge, the triage top-3 covers them). Operator decision
    2026-10-28 (ask me less): NAVIGATOR_DECISIONS_NUDGES=0/false turns these reminders
    off (digest only); unset/1/true keeps them.
  one digest at the digest hour local (NAVIGATOR_DIGEST_HOUR, 0-23; default DIGEST_HOUR),
    only when something is waiting. While a source fails to list, the digest is neither
    sent nor marked (an incomplete list must not spend the day's digest); from the
    fallback hour - digest hour + 2, capped at 22 so a late digest hour never pushes the
    fallback into the quiet hours - it goes out anyway: the items that could be listed,
    if any, plus one line '(could not list: <kinds>)'. While the digest is held no
    reminder is sent either (the held digest names those items at the fallback hour),
    so an item is never sent twice in one morning.
Nothing is sent in the quiet hours (QUIET_START_HOUR..QUIET_END_HOUR local) EXCEPT at
the digest hour, which is never quiet (a 7am digest must get through QUIET_END_HOUR=8).
A NAVIGATOR_DIGEST_HOUR / NAVIGATOR_DECISIONS_NUDGES value that does not parse raises
ValueError naming the variable (reported per cycle as decisions_error) before any state
write. U2b: the daemon passes its Settings to decisions_tick when the settings document
(navigator/settings) sets digest_hour / decisions_nudges / decisions_tz; a non-None field
beats the environment variable, None keeps the env path above.

Only decisions_tick (the daemon cycle, __main__._decisions_step) writes the state file:
first_seen per item key, the local day each item was last nudged, and the local day of
the last digest. The refreshed state (new keys, vanished keys pruned) is written BEFORE
any send and the advanced marks again after; when the file cannot be written nothing is
sent at all - a state that never advances would otherwise repeat the digest every cycle.
Keys of a kind whose source failed to list (an ACT hiccup) are kept as they were, so a
blip never resets an item's age or lets it be nudged twice. A merge key whose run is
still open at awaiting_review (act.list_open_runs) is kept too: cli.is_audited fails
closed WITHOUT raising when ACT events cannot be listed, so the run merely vanishes
from the candidates for a tick; when list_open_runs itself fails, 'merge' counts as a
failed source.

Retries: a notifier whose send raises makes decisions_tick raise before the digest/nudge
marks are advanced, so the next cycle sends again. The production TelegramNotifier.send
is best-effort (observability/notify.py swallows errors): a message Telegram lost is NOT
retried, because the notifier never reports it. The state-not-advanced retry applies
only to a notifier whose send raises.

U3 (design D3): snapshot() is what the daemon publishes each cycle to the ACT document
navigator/decisions (__main__._decisions_step): the same items collect_with_status
returns, each with the exact actions the Telegram words accept, plus the runs at the
merge gate that are not audited yet (awaiting_audit, shown without buttons). A due digest
is also kept as navigator/digest/latest plus one navigator.digest event, with the outcomes
merged / failed since the previous digest (tracked by the last outcome id seen).

This module must not import telegram_proposals (telegram_proposals imports it).
"""
import json
import os
import re
import sys
import tempfile
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from navigator.contracts.enums import ApprovalDecision, ApprovalGate, ProposalStatus, RunPhase
from navigator.contracts.event import Event
from navigator.observability import actions
from navigator.observability.logging import get_logger
from navigator.observability.telegram_inbox import _candidates, _reply_arg, short_name
from navigator.runtime.outcome_store import OutcomeStore

log = get_logger("navigator.decisions")

DEFAULT_STATE_PATH = "/Users/Manoj/calwbot/logs/navigator/decisions_state.json"
DEFAULT_MANUAL_PATH = "/Users/Manoj/calwbot/logs/navigator/manual_decisions.json"
NUDGE_AFTER_S = 4 * 3600
DIGEST_HOUR = 8
QUIET_START_HOUR = 23
QUIET_END_HOUR = 8
DEFAULT_TZ = "America/New_York"
MAX_PROPOSALS_LISTED = 5
MAX_HOW_CHARS = 300
CHUNK = 3900
# manual item ids: the same shape as a proposal short name
MANUAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
# the kinds the 4h nudge applies to (proposals never nudge)
NUDGE_KINDS = ("merge", "manual")
# U3: the ACT documents / event published for the UI
DECISIONS_DOC = "navigator/decisions"
DIGEST_DOC = "navigator/digest/latest"
DIGEST_EVENT = "navigator.digest"
DOC_ACTOR = "navigator-daemon"
DEFAULT_OUTCOME_PATH = "/tmp/navigator-outcomes.jsonl"
OUTCOMES_SCANNED = 200
OUTCOMES_FALLBACK = 20
# the actions the Telegram words accept for each kind (manual steps have no button)
KIND_ACTIONS = {"merge": ("merge", "reject"), "proposal": ("approve", "decline"),
                "manual": ()}


def state_path() -> str:
    return os.getenv("NAVIGATOR_DECISIONS_STATE_PATH") or DEFAULT_STATE_PATH


def manual_path() -> str:
    return os.getenv("NAVIGATOR_MANUAL_DECISIONS_PATH") or DEFAULT_MANUAL_PATH


def local_tz(settings=None):
    """settings.decisions_tz when set (U2b), else NAVIGATOR_DECISIONS_TZ / DEFAULT_TZ."""
    if settings is not None and getattr(settings, "decisions_tz", None) is not None:
        return zoneinfo.ZoneInfo(settings.decisions_tz)
    return zoneinfo.ZoneInfo(os.getenv("NAVIGATOR_DECISIONS_TZ") or DEFAULT_TZ)


def _bad_env(name: str, raw: str, expected: str) -> None:
    """Log the full message, then raise: _decisions_step in __main__.py reports only
    type(e).__name__, so without this line the operator would see
    `decisions_error: ValueError` every cycle and never learn which variable."""
    msg = f"{name} must be {expected}, got {raw!r}"
    log.warning("decisions.bad_env", var=name, error=msg)
    raise ValueError(msg)


def digest_hour(settings=None) -> int:
    """settings.digest_hour when set (U2b), else NAVIGATOR_DIGEST_HOUR (local hour 0-23 of
    the morning digest; default DIGEST_HOUR). An env value that is not an integer in
    0..23 raises ValueError naming the variable (reported per cycle as decisions_error)."""
    if settings is not None and getattr(settings, "digest_hour", None) is not None:
        return settings.digest_hour
    raw = os.getenv("NAVIGATOR_DIGEST_HOUR")
    if raw is None or not raw.strip():
        return DIGEST_HOUR
    try:
        hour = int(raw.strip())
    except ValueError:
        _bad_env("NAVIGATOR_DIGEST_HOUR", raw, "an integer hour 0-23")
    if not 0 <= hour <= 23:
        _bad_env("NAVIGATOR_DIGEST_HOUR", raw, "an integer hour 0-23")
    return hour


def nudges_enabled(settings=None) -> bool:
    """settings.decisions_nudges when set (U2b), else NAVIGATOR_DECISIONS_NUDGES:
    '1'/'true'/unset = the 4h reminders are sent; '0'/'false' = digest only. Anything
    else raises ValueError naming the variable."""
    if settings is not None and getattr(settings, "decisions_nudges", None) is not None:
        return settings.decisions_nudges
    raw = (os.getenv("NAVIGATOR_DECISIONS_NUDGES") or "1").strip().lower()
    if raw in ("1", "true"):
        return True
    if raw in ("0", "false"):
        return False
    _bad_env("NAVIGATOR_DECISIONS_NUDGES", os.getenv("NAVIGATOR_DECISIONS_NUDGES") or "",
             "1/true or 0/false")
    return True  # unreachable: _bad_env raises


@dataclass(frozen=True)
class Decision:
    kind: str   # "merge" | "proposal" | "manual"
    key: str    # stable: "merge:<run id>", "proposal:<proposal id>", "manual:<item id>"
    title: str
    reply: str  # exactly what the operator sends / does
    since: datetime | None  # tz-aware; None when the source does not know


# ---- small helpers -------------------------------------------------------------

def _aware(dt) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_time(value) -> datetime | None:
    """ISO string (or datetime) -> aware datetime (naive = UTC); None when unusable."""
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _flat(text) -> str:
    """One line: Telegram list lines must not contain newlines."""
    return " ".join(str(text).split())


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path, data) -> None:
    """Atomic replace (the loop.write_quota_pause pattern); OSErrors propagate."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".decisions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- manual items file ----------------------------------------------------------

def read_manual(path=None) -> list[dict]:
    """The tracked manual steps: [{'id', 'title', 'how', 'added'}], plus 'script' and
    'sha256' (the pinned action, actions.py) when present as str. A missing, unreadable
    or garbage file, or one of the wrong shape, is []; entries without a str id/title/how
    are skipped ('added' is kept when it is a str, else None)."""
    data = _read_json(path or manual_path())
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        if not all(isinstance(it.get(k), str) for k in ("id", "title", "how")):
            continue
        added = it.get("added")
        entry = {"id": it["id"], "title": it["title"], "how": it["how"],
                 "added": added if isinstance(added, str) else None}
        for k in ("script", "sha256"):  # the pinned action; absent when not pinned
            if isinstance(it.get(k), str):
                entry[k] = it[k]
        out.append(entry)
    return out


def add_manual(path, item_id: str, title: str, how: str, now=None, script=None) -> None:
    """Track a manual step; an item with the same id is replaced in place. Atomic write.
    With `script` (a path) the step carries ONE pinned action: its absolute path and
    sha256 are stored once actions.check_script accepts it (ValueError carrying the
    fixed reason otherwise); the operator runs it with 'run <id>' in Telegram."""
    if not isinstance(item_id, str) or not MANUAL_ID.fullmatch(item_id):
        raise ValueError(f"bad manual item id {item_id!r}")
    now = _aware(now) or datetime.now(timezone.utc)
    entry = {"id": item_id, "title": str(title), "how": str(how),
             "added": now.isoformat(timespec="seconds")}
    if script is not None:
        abs_path = os.path.abspath(script)
        reason = actions.check_script(abs_path, actions.actions_dir())
        if reason is not None:
            raise ValueError(reason)
        try:
            digest = actions.sha256_file(abs_path)
        except OSError:
            raise ValueError("missing")
        entry["script"] = abs_path
        entry["sha256"] = digest
    items = read_manual(path)
    for i, it in enumerate(items):
        if it["id"] == item_id:
            items[i] = entry
            break
    else:
        items.append(entry)
    _write_json(path, {"items": items})


def remove_manual(path, item_id: str) -> bool:
    """True when an item was removed; the file is only rewritten when something changed."""
    items = read_manual(path)
    kept = [it for it in items if it["id"] != item_id]
    if len(kept) == len(items):
        return False
    _write_json(path, {"items": kept})
    return True


# ---- collecting -----------------------------------------------------------------

def _by_age(items: list[Decision]) -> list[Decision]:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda d: (d.since is None, d.since or floor))


def _merge_decisions(cands) -> list[Decision]:
    return [Decision("merge", f"merge:{run.id}", _flat(title) or run.id,
                     f"merge {_reply_arg(run, short)}", None)
            for run, short, title in cands]


def _manual_decisions(items: list[dict]) -> list[Decision]:
    out = []
    for it in items:
        how = _flat(it["how"])
        if len(how) > MAX_HOW_CHARS:
            how = how[:MAX_HOW_CHARS] + "..."
        reply = (f"run {it['id']} (send 'show {it['id']}' to read the script)"
                 if it.get("script") else f"do yourself: {how}")
        out.append(Decision("manual", f"manual:{it['id']}", _flat(it["title"]) or it["id"],
                            reply, _parse_time(it.get("added"))))
    return _by_age(out)


def _proposal_decisions(props) -> list[Decision]:
    out = []
    for p in props:
        name = short_name(p.title or "") or p.id
        out.append(Decision("proposal", f"proposal:{p.id}", _flat(p.title) or p.id,
                            f"approve {name} / decline {name}", _aware(p.created_at)))
    return _by_age(out)


def collect_with_status(act, manual_file=None, candidates=None) -> tuple[list[Decision], set[str]]:
    """Everything waiting on the operator - merge items first, then manual, then
    proposals; oldest first within a kind (unknown age last) - plus the set of kinds
    whose source raised. One failed listing never hides the others, and decisions_tick
    leaves a failed kind's recorded state exactly as it was."""
    sources = (
        ("merge", lambda: _merge_decisions((candidates or _candidates)(act))),
        ("manual", lambda: _manual_decisions(read_manual(manual_file or manual_path()))),
        ("proposal", lambda: _proposal_decisions(act.list_proposals(ProposalStatus.proposed))),
    )
    items: list[Decision] = []
    failed: set[str] = set()
    for kind, load in sources:
        try:
            items.extend(load())
        except Exception as e:
            log.warning("decisions.source_failed", source=kind, error=type(e).__name__)
            failed.add(kind)
    return items, failed


def collect(act, manual_file=None, candidates=None) -> list[Decision]:
    return collect_with_status(act, manual_file, candidates)[0]


# ---- state file (written by the daemon tick only) ------------------------------

def _str_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def read_state(path) -> dict:
    """{'first_seen': {key: ISO}, 'nudged': {key: 'YYYY-MM-DD' local}, 'digest':
    'YYYY-MM-DD' local or None}; a missing or garbage file gives the empty defaults."""
    data = _read_json(path)
    data = data if isinstance(data, dict) else {}
    digest = data.get("digest")
    return {"first_seen": _str_map(data.get("first_seen")),
            "nudged": _str_map(data.get("nudged")),
            "digest": digest if isinstance(digest, str) else None}


def write_state(path, state: dict) -> bool:
    """Atomic write; True when written. An OSError is logged by class name and gives
    False (the caller then sends nothing: a state that cannot advance must not spam)."""
    try:
        _write_json(path, state)
    except OSError as e:
        log.warning("decisions.state_write_failed", error=type(e).__name__)
        return False
    return True


# ---- formatting -----------------------------------------------------------------

def _chunks(lines: list[str]) -> list[str]:
    out, cur = [], ""
    for line in lines:
        line = line[:CHUNK]
        if cur and len(cur) + len(line) + 1 > CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _age(item: Decision, now: datetime, first_seen: dict) -> str:
    ts = _parse_time(first_seen.get(item.key)) or item.since
    if ts is None:
        return "new"
    hours = int((now - ts).total_seconds() // 3600)
    return f"waiting {hours}h" if hours >= 1 else "waiting <1h"


def format_decisions(items: list[Decision], now: datetime, first_seen: dict | None = None,
                     header: str = "Waiting on you") -> list[str]:
    """Telegram-sized chunks (each <= CHUNK): a header line, then one numbered line per
    item 'kind: title - waiting <H>h - reply'. The age comes from first_seen (the daemon's
    record) or the item's own since; 'new' when neither is known. Proposals past
    MAX_PROPOSALS_LISTED fold into '+N more proposals - send all'."""
    if not items:
        return ["Nothing is waiting on you."]
    first_seen = first_seen or {}
    lines = [f"{header} ({len(items)}):"]
    shown_props = folded = 0
    for i, d in enumerate(items, 1):
        if d.kind == "proposal":
            shown_props += 1
            if shown_props > MAX_PROPOSALS_LISTED:
                folded += 1
                continue
        lines.append(f"{i}. {d.kind}: {_flat(d.title)} - {_age(d, now, first_seen)} - "
                     f"{_flat(d.reply)}")
    if folded:
        lines.append(f"+{folded} more proposals - send all")
    return _chunks(lines)


# ---- the daemon tick ------------------------------------------------------------

def _quiet(local: datetime, digest_at: int | None = None) -> bool:
    """True in the quiet hours; the digest hour itself is never quiet."""
    hour = DIGEST_HOUR if digest_at is None else digest_at
    if local.hour == hour:
        return False
    return local.hour >= QUIET_START_HOUR or local.hour < QUIET_END_HOUR


def _due(first_seen_iso, now: datetime) -> bool:
    ts = _parse_time(first_seen_iso)
    return ts is not None and (now - ts).total_seconds() >= NUDGE_AFTER_S


def decisions_tick(act, notifier, now: datetime | None = None, state_file=None,
                   manual_file=None, candidates=None, settings=None) -> dict:
    """One daemon-cycle pass over the decisions list: record first_seen for new items,
    prune vanished ones (keeping every key of a kind whose source failed, and every merge
    key whose run is still open at awaiting_review - an audit-check hiccup drops the run
    from the candidates without raising), then - outside the quiet hours - send the day's
    digest once (from the digest hour local - NAVIGATOR_DIGEST_HOUR, default DIGEST_HOUR,
    an hour that is never quiet - only when something waits) and, unless
    NAVIGATOR_DECISIONS_NUDGES is 0/false, nudge merge/manual items that have waited
    NUDGE_AFTER_S and were not nudged today (never right after a digest). While a source
    fails to list, the digest is neither sent nor marked before the fallback hour
    (digest hour + 2, capped at 22); from then on it is marked and sent (the listed
    items, if any) with one extra message '(could not list: <kinds>)' naming the failed
    kinds. While the digest is held no reminder is sent either: the held digest names
    those items at the fallback hour, so an item is not sent twice. A bad value of
    either variable raises ValueError naming it before any state write. A failing
    act.list_open_runs counts 'merge' as a failed source. The refreshed state is written BEFORE
    any send: when that write fails nothing is sent and {'items', 'state_write_failed':
    True} is returned, so an unwritable state file never turns into a digest every cycle.
    After the sends the advanced marks are written; a failure there is reported the same
    way. Returns {'items', 'digest_sent', 'nudged'} or {'quiet': True, 'items'}.

    A notifier whose send raises makes this raise (the caller logs the class name) with
    the digest/nudge marks NOT advanced, so the next cycle sends again. The production
    TelegramNotifier.send is best-effort (observability/notify.py swallows errors): a
    message Telegram lost is not retried, because the notifier never tells us. The retry
    applies only to a notifier whose send raises."""
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware")
    # a bad value raises here, before any state write; `settings` (U2b) wins over the
    # environment for each field it sets
    digest_at = digest_hour(settings)
    nudges = nudges_enabled(settings)
    # a digest hour of 21-23 would put +2 at 23-25 (quiet / never): cap the fallback
    fallback_at = min(digest_at + 2, 22)
    local = now.astimezone(local_tz(settings))
    today = local.date().isoformat()
    path = state_file or state_path()
    items, failed = collect_with_status(act, manual_file, candidates)
    failed = set(failed)  # a local copy: never mutate what collect_with_status returned
    try:
        open_review = {r.id for r in act.list_open_runs()
                       if r.phase == RunPhase.awaiting_review}
    except Exception as e:
        log.warning("decisions.open_runs_failed", error=type(e).__name__)
        open_review = set()
        failed.add("merge")
    live = {d.key for d in items}

    def keep(key: str) -> bool:
        # a kind that failed to list contributes no items this tick; its keys stay
        if key in live or key.split(":", 1)[0] in failed:
            return True
        # a merge item whose run is still at the gate: cli.is_audited failed closed
        # (no raise) on an events hiccup, so the run merely vanished from the candidates
        return key.startswith("merge:") and key[len("merge:"):] in open_review

    old = read_state(path)
    first_seen = {k: v for k, v in old["first_seen"].items() if keep(k)}
    nudged = {k: v for k, v in old["nudged"].items() if keep(k)}
    for d in items:
        first_seen.setdefault(d.key, now.isoformat(timespec="seconds"))
    state = {"first_seen": first_seen, "nudged": nudged, "digest": old["digest"]}
    n = len(items)
    if not write_state(path, state):
        return {"items": n, "state_write_failed": True}
    if _quiet(local, digest_at):
        return {"quiet": True, "items": n}
    digest_sent = False
    publish_error = None
    # with a failed source the digest waits (unsent, unmarked) until the fallback hour,
    # then goes out naming the kinds it could not list
    digest_due = (local.hour >= digest_at and state["digest"] != today
                  and (not failed or local.hour >= fallback_at))
    # while the digest is held (a failed source, digest_at..fallback_at, not yet sent
    # today) no reminder goes out either: the held digest will name those items
    held = (bool(failed) and digest_at <= local.hour < fallback_at
            and state["digest"] != today)
    if digest_due:
        state["digest"] = today
        sent_chunks: list[str] = []
        if items:
            for text in format_decisions(items, now, first_seen,
                                         header="Morning digest - waiting on you"):
                notifier.send(text)
                sent_chunks.append(text)
            digest_sent = True
            for d in items:  # the digest already named them: no nudge right after it
                if d.kind in NUDGE_KINDS:
                    nudged[d.key] = today
        if failed:
            notifier.send("(could not list: " + ", ".join(sorted(failed)) + ")")
        # U3: keep the digest for the UI (never raises; marks/messages unaffected)
        publish_error = _publish_digest(act, items, sent_chunks, now, settings, failed)
    # NAVIGATOR_DECISIONS_NUDGES=0: digest only (the digest's own nudged marks still
    # happened above, so turning reminders back on never repeats a digested item)
    due = [] if held or not nudges else [d for d in items if d.kind in NUDGE_KINDS
                                          and nudged.get(d.key) != today
                                          and _due(first_seen.get(d.key), now)]
    if due:
        for text in format_decisions(due, now, first_seen,
                                     header="Reminder - still waiting on you"):
            notifier.send(text)
        for d in due:
            nudged[d.key] = today
    result = {"items": n, "digest_sent": digest_sent, "nudged": len(due)}
    if publish_error is not None:
        result["digest_publish_error"] = publish_error
    if not write_state(path, state):
        result["state_write_failed"] = True
    return result


# ---- U3: published documents ------------------------------------------------------

def _try(fn, default=None):
    try:
        return fn()
    except Exception as e:
        log.warning("decisions.snapshot_part_failed", error=type(e).__name__)
        return default


def _repo_of(act, proposal_id):
    if not proposal_id:
        return None
    prop = _try(lambda: act.get_proposal(proposal_id))
    repo = getattr(prop, "repo", None)
    return repo if isinstance(repo, str) else None


def _tier_of(act, run):
    from navigator.cli import merge_tier_of
    tier = _try(lambda: merge_tier_of(act, run))
    return tier if tier in ("A", "B", "C") else None


def _pending_merge(act, run_id):
    return act.list_approvals(target=run_id, gate=ApprovalGate.merge,
                              decision=ApprovalDecision.pending)


def _snapshot_item(act, d: Decision, runs_by_id: dict, hows: dict) -> dict:
    ident = d.key.partition(":")[2]
    entry = {"key": d.key, "kind": d.kind, "title": d.title, "reply": d.reply,
             "since": d.since.isoformat() if d.since is not None else None,
             "repo": None, "run_id": None, "proposal_id": None, "approval_id": None,
             "tier": None, "actions": list(KIND_ACTIONS.get(d.kind, ())), "how": None}
    if d.kind == "merge":
        entry["run_id"] = ident
        pending = _try(lambda: _pending_merge(act, ident), []) or []
        entry["approval_id"] = pending[0].id if pending else None
        run = runs_by_id.get(ident)
        if run is not None:
            entry["proposal_id"] = run.proposal_id
            entry["repo"] = _repo_of(act, run.proposal_id)
            entry["tier"] = _tier_of(act, run)
    elif d.kind == "proposal":
        entry["proposal_id"] = ident
        entry["repo"] = _repo_of(act, ident)
    elif d.kind == "manual":
        how = hows.get(ident)
        entry["how"] = how if isinstance(how, str) else None
    return entry


def _awaiting_audit(act, open_runs, merge_keys: set) -> list[dict]:
    """Runs at the merge gate (awaiting_review + pending merge approval) that are not
    merge candidates, i.e. not audited. No extra is_audited call."""
    out = []
    for run in open_runs:
        if run.phase != RunPhase.awaiting_review or f"merge:{run.id}" in merge_keys:
            continue
        if not _pending_merge(act, run.id):
            continue
        prop = _try(lambda: act.get_proposal(run.proposal_id))
        title = _flat(getattr(prop, "title", "") or "") or run.id
        repo = getattr(prop, "repo", None)
        out.append({"run_id": run.id, "proposal_id": run.proposal_id, "title": title,
                    "repo": repo if isinstance(repo, str) else None,
                    "tier": _tier_of(act, run)})
    return out


def snapshot(act, now: datetime | None = None, manual_file=None, candidates=None) -> dict:
    """The navigator/decisions document body: collect_with_status's items, each with the
    actions the Telegram words accept, plus awaiting_audit (unaudited runs at the merge
    gate, no buttons) and the sorted kinds that failed to list. Never raises."""
    now = _aware(now) or datetime.now(timezone.utc)
    try:
        items, failed = collect_with_status(act, manual_file, candidates)
        failed = set(failed)
    except Exception as e:
        log.warning("decisions.snapshot_collect_failed", error=type(e).__name__)
        items, failed = [], {"merge", "manual", "proposal"}
    try:
        open_runs = list(act.list_open_runs())
    except Exception as e:
        log.warning("decisions.snapshot_open_runs_failed", error=type(e).__name__)
        open_runs = None
    runs_by_id = {r.id: r for r in open_runs or []}
    hows = {}
    if any(d.kind == "manual" for d in items):
        hows = _try(lambda: {it["id"]: it["how"]
                             for it in read_manual(manual_file or manual_path())}, {}) or {}
    out = []
    for d in items:
        try:
            out.append(_snapshot_item(act, d, runs_by_id, hows))
        except Exception as e:
            log.warning("decisions.snapshot_item_failed", kind=d.kind, error=type(e).__name__)
            failed.add(d.kind)
    awaiting: list[dict] = []
    if open_runs is None or "merge" in failed:
        failed.add("awaiting_audit")
    else:
        try:
            awaiting = _awaiting_audit(act, open_runs,
                                       {d.key for d in items if d.kind == "merge"})
        except Exception as e:
            log.warning("decisions.awaiting_audit_failed", error=type(e).__name__)
            awaiting = []
            failed.add("awaiting_audit")
    return {"ts": now.isoformat(timespec="seconds"), "items": out,
            "awaiting_audit": awaiting, "failed": sorted(failed)}


def digest_body(items: list[Decision], chunks: list[str], now, outcomes_since: list,
                previous_through: str | None, could_not_list=None, settings=None) -> dict:
    """The navigator/digest/latest body. outcomes_since is newest-first."""
    now = _aware(now) or datetime.now(timezone.utc)
    merged = [{"run_id": o.run_id, "proposal_id": o.proposal_id, "summary": o.summary}
              for o in outcomes_since if getattr(o, "status", None) == "completed"]
    failed = [{"run_id": o.run_id, "proposal_id": o.proposal_id, "summary": o.summary,
               "reason_code": getattr(o, "reason_code", None)}
              for o in outcomes_since if getattr(o, "status", None) == "failed"]
    through = getattr(outcomes_since[0], "id", None) if outcomes_since else previous_through
    return {"date": now.astimezone(local_tz(settings)).date().isoformat(),
            "sent_at": now.isoformat(timespec="seconds"),
            "chunks": list(chunks),
            "items": [{"key": d.key, "kind": d.kind, "title": d.title, "reply": d.reply}
                      for d in items],
            "could_not_list": sorted(could_not_list or []),
            "merged_since_last": merged, "failed_since_last": failed,
            "outcomes_through": through}


def _outcome_path(settings=None) -> str:
    path = getattr(settings, "outcome_path", None) if settings is not None else None
    return path or os.getenv("NAVIGATOR_OUTCOME_PATH") or DEFAULT_OUTCOME_PATH


def _publish_digest(act, items, sent_chunks, now, settings=None, failed=None) -> str | None:
    """Write navigator/digest/latest and emit one navigator.digest event. Returns None, or
    the class name of the exception that stopped it (never raises). A previous digest
    dated today (a re-sent digest after a state-write failure) is left alone. An act
    without documents (a bare test double) publishes nothing."""
    try:
        if not (callable(getattr(act, "put_doc", None))
                and callable(getattr(act, "get_doc", None))):
            return None
        today = now.astimezone(local_tz(settings)).date().isoformat()
        prev = act.get_doc(DIGEST_DOC)
        prev_body = prev[0] if prev is not None else None
        if not isinstance(prev_body, dict):
            prev_body = None
        if prev_body is not None and prev_body.get("date") == today:
            return None
        previous_through = prev_body.get("outcomes_through") if prev_body else None
        if not isinstance(previous_through, str):
            previous_through = None
        outcomes = OutcomeStore(_outcome_path(settings)).latest(OUTCOMES_SCANNED)
        kept = None
        if previous_through is not None:
            for i, o in enumerate(outcomes):
                if o.id == previous_through:
                    kept = outcomes[:i]
                    break
        if kept is None:  # no previous digest, or its id is gone (file wiped / > 200 newer)
            kept = outcomes[:OUTCOMES_FALLBACK]
        body = digest_body(items, sent_chunks, now, kept, previous_through,
                           could_not_list=failed, settings=settings)
        act.put_doc(DIGEST_DOC, body, actor=DOC_ACTOR)
        act.emit_event(Event(kind=DIGEST_EVENT, correlation_id=None, payload=body))
    except Exception as e:
        log.warning("decisions.digest_publish_failed", error=type(e).__name__)
        return type(e).__name__
    return None


# ---- CLI: track manual steps ----------------------------------------------------

USAGE = ("usage: python -m navigator.observability.decisions "
         "add <id> <title> <how> [--script <path>] | done <id> | list")


def _cli(argv: list[str]) -> int:
    scripted = len(argv) == 6 and argv[0] == "add" and argv[4] == "--script"
    if scripted or (len(argv) == 4 and argv[0] == "add"):
        try:
            add_manual(manual_path(), argv[1], argv[2], argv[3],
                       script=argv[5] if scripted else None)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print("added")
        return 0
    if len(argv) == 2 and argv[0] == "done":
        removed = remove_manual(manual_path(), argv[1])
        print("removed" if removed else "not found")
        return 0 if removed else 1
    if argv == ["list"]:
        items = _manual_decisions(read_manual(manual_path()))
        for chunk in format_decisions(items, datetime.now(timezone.utc)):
            print(chunk)
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
