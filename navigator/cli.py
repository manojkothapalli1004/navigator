"""Operator commands: `python -m navigator <command> [id]`.

Every decision here is recorded in ACT; the daemon acts on it next cycle.
`audited <run-id> [--note TEXT]` marks a run awaiting review as audited (a
'run.audited' event carrying the run's verified_sha plus an approved review-gate
approval targeting the run); only audited runs accept a merge decision from
Telegram. The CLI `merge` does not need it.
`reject <run-id> [--reason CODE]` declines the merge; the optional CODE (one of
REASON_CODES) is stored as the approval note `reason=CODE` and recorded by the
daemon as the declined outcome's reason_code.
`inbox [--since UPDATE_ID]` prints the Telegram inbox file (JSON lines, newest
last) so other tools read operator messages from the file instead of calling
getUpdates themselves.
`settings` (U2b) shows the ACT settings document navigator/settings next to what the
daemon reports in navigator/status; `settings set <field> <value>` / `settings unset
<field>` write it with optimistic versioning (actor 'operator'), refusing env-only
and invalid fields exactly as Settings.overlay does. The daemon applies a change at
its next cycle.
`cleanup [--apply]` is the exception: it removes finished runs' worktrees and
nav/run-* branches directly (dry run unless --apply).
`doctor [--plist PATH]` is read-only: it replays the daemon's startup against the
launchd plist (ONLY its EnvironmentVariables, cwd = its WorkingDirectory):
Settings.from_env, effective_repos, the driver choice, the SDK version (only for
driver=claude_sdk) and the navigator git HEAD. When an ACT client is available it
also lists pending merge approvals of open runs that have been waiting longer than
NAVIGATOR_STALE_APPROVAL_HOURS (default 24; read from the operator's shell env) as
WARN lines, one per approval, naming the run: such a request may have been
forgotten, or a rebase since then may have invalidated it. The Approval contract
records no creation time, so the age is taken from the ULID in the approval id.
No approval is ever modified. It also reports, per configured repo, whether the
daemon's open-proposal backlog has reached NAVIGATOR_MAX_OPEN_PROPOSALS (counted
like the daemon: that repo only, proposals older than NAVIGATOR_PROPOSAL_TTL_DAYS
ignored, nothing is expired), and warns when both the legacy navigator-telegram
and the one-bot telegram-bot launchd plists are installed; this is file presence
only, so a job that is still loaded after its plist was renamed is not detected.

What doctor prints for env-derived values (all through _safe()):
- env vars whose key has a sensitive marker, or that are neither NAVIGATOR_* nor
  PATH, always print as '<redacted>';
- a value of the form scheme://... prints as scheme://host[:port], plus '/...' if
  anything followed the authority (userinfo, path, query, fragment are dropped);
  if that form is ambiguous it prints as '<redacted>';
- any other value containing '@', '%40', '?' or '://', or starting with
  'scheme:/', prints as '<redacted>';
- every other value (e.g. a plain filesystem path) prints unchanged.
In every printed value and env key, control characters (CR, LF, tab, NUL, line/
paragraph separators, ...) are replaced with '?', so a value cannot forge an
extra output line.
"""
import json
import math
import os
import plistlib
import re
import subprocess
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO
from ulid import ULID
from navigator.clients.act_client import _parse_ts
from navigator.observability.logging import get_logger
from navigator.observability.notify import format_verification
from navigator.contracts.enums import (ApprovalDecision, ApprovalGate, ProposalStatus,
                                       RunPhase, RunStatus)
from navigator.contracts.event import Event

COMMANDS = ("status", "approve", "decline", "diff", "merge", "reject", "retry", "cleanup",
            "doctor", "audited", "inbox", "settings")
ACTOR = "operator"
AUDITOR = "auditor"
# merge-gate decline reasons accepted by `navigator reject <run> --reason CODE`
REASON_CODES = ("wrong_behavior", "unsafe", "scope_creep", "docs_wrong", "tests_weak",
                "low_value", "duplicate", "rebuild_rev2", "other")
_log = get_logger("navigator.cli")

# run statuses whose worktree/branch can go regardless of age
_FINISHED_STATUSES = {"completed", "declined", "aborted"}
# failed runs are kept this long for inspection
_FAILED_GRACE = timedelta(days=7)
_BRANCH_PREFIX = "nav/run-"
_ALWAYS_PROTECTED = {"main", "master"}


def run_cli(argv: list[str], *, act, out: TextIO, worktree_root: str | None = None) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: navigator {{{'|'.join(COMMANDS)}}} [id]  "
              f"(cleanup takes no id: navigator cleanup [--apply]; "
              f"navigator doctor [--plist PATH]; "
              f"navigator audited <run-id> [--note TEXT]; "
              f"navigator reject <run-id> [--reason CODE]; "
              f"navigator inbox [--since UPDATE_ID]; "
              f"navigator settings | navigator settings set <field> <value> | "
              f"navigator settings unset <field>)", file=out)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        return _status(act, out)
    if cmd == "settings":
        return _settings(act, args, out)
    if cmd == "doctor":
        return _doctor(args, out, act=act)
    if cmd == "audited":
        return _audited(act, args, out)
    if cmd == "inbox":
        return _inbox(args, out)
    if cmd == "cleanup":
        if args not in ([], ["--apply"]):
            print("usage: navigator cleanup [--apply]", file=out)
            return 2
        return _cleanup(act, apply=bool(args), out=out, worktree_root=worktree_root)
    if cmd == "reject" and len(args) == 3 and args[1] == "--reason":
        if args[2] not in REASON_CODES:
            print(f"unknown reason {args[2]!r}; one of: " + ", ".join(REASON_CODES), file=out)
            return 2
        return _decide_merge(act, args[0], out, ApprovalDecision.declined,
                             note=f"reason={args[2]}")
    if len(args) != 1:
        print(f"navigator {cmd} needs exactly one id", file=out)
        return 2
    return _HANDLERS[cmd](act, args[0], out)


def _status(act, out) -> int:
    proposed = act.list_proposals(ProposalStatus.proposed)
    approved = act.list_proposals(ProposalStatus.approved)
    print(f"proposed ({len(proposed)}):", file=out)
    for p in proposed:
        print(f"  {p.id}  {p.title}  [{', '.join(p.affected_files)}]", file=out)
    print(f"approved ({len(approved)}):", file=out)
    for p in approved:
        runs = act.list_runs_for_proposal(p.id)
        state = ", ".join(f"{r.id}:{r.status.value}" for r in runs) or "no run yet"
        print(f"  {p.id}  {p.title}  ({state})", file=out)
    runs = act.list_open_runs()
    print(f"open runs ({len(runs)}):", file=out)
    for r in runs:
        print(f"  {r.id}  {r.phase.value}  branch={r.branch}", file=out)
    return 0


def _set_proposal(act, pid, out, status: ProposalStatus) -> int:
    p = act.get_proposal(pid)
    if p is None:
        print(f"no such proposal: {pid}", file=out)
        return 1
    if p.status != ProposalStatus.proposed:
        print(f"{pid} is {p.status.value}, not proposed", file=out)
        return 1
    act.set_proposal_status(pid, status)
    print(f"{pid} -> {status.value}", file=out)
    return 0


def _decide_merge(act, run_id, out, decision: ApprovalDecision,
                  note: str | None = None) -> int:
    pending = act.list_approvals(target=run_id, gate=ApprovalGate.merge,
                                 decision=ApprovalDecision.pending)
    if not pending:
        print(f"no pending merge approval for {run_id}", file=out)
        return 1
    # note is passed only when given: some client wrappers take no note argument
    if note is not None:
        act.decide_approval(pending[0].id, decision, actor=ACTOR, note=note)
    else:
        act.decide_approval(pending[0].id, decision, actor=ACTOR)
    print(f"{run_id} merge -> {decision.value} (applied on the daemon's next cycle)", file=out)
    return 0


def _find_run(act, run_id):
    return next((r for r in act.list_open_runs() if r.id == run_id), None)


# the only event kinds is_audited looks at
_AUDIT_EVENT_KINDS = frozenset({"run.audited", "run.awaiting_review"})
# how many client wrappers (.inner) to unwrap when looking for list_events
_MAX_WRAPPER_DEPTH = 8
# events are only read from this long before the run started: the run's own
# events cannot predate it by more than clock skew between navigator and ACT
_AUDIT_SINCE_SKEW = timedelta(minutes=5)


def _audit_since(run) -> datetime | None:
    """Lower bound for the ACT events is_audited needs: the run's started_at minus a
    skew allowance. None when the run has no usable started_at (read everything)."""
    started = _parse_ts(getattr(run, "started_at", None))
    return started - _AUDIT_SINCE_SKEW if started is not None else None


def _events(act, since: datetime | None = None):
    """Audit-relevant navigator events read back from ACT (only those not before
    `since` when given), or None when they cannot be listed (a client without
    list_events, or the call failed; the failure is logged as 'cli.events_unreadable'
    with the exception CLASS name only). Callers treat None as 'not audited' (fail
    closed). Wrappers such as NotifyingACT are unwrapped through their .inner
    attribute."""
    target = act
    for _ in range(_MAX_WRAPPER_DEPTH):
        if hasattr(target, "list_events"):
            break
        target = getattr(target, "inner", None)
        if target is None:
            return None
    else:
        return None
    try:
        return list(target.list_events(kinds=set(_AUDIT_EVENT_KINDS), since=since))
    except Exception as e:
        # class name only: an exception message may carry URLs or response bodies
        _log.warning("cli.events_unreadable", error=type(e).__name__)
        return None


# one timestamp parser for the whole audit path (datetime or ISO string -> aware
# UTC, naive means UTC, None when unusable); kept under its old name for importers
_as_utc = _parse_ts


def _payload(event) -> dict:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, dict) else {}


def is_audited(act, run) -> bool:
    """True once the run's CURRENT verified_sha has been audited (`navigator audited`).

    `run` is a Run or the id of an open run. All of the following, read back from
    ACT, are required:
    - a 'run.audited' event for the run whose payload verified_sha equals the run's
      verified_sha (None never matches: an unverified run cannot be audited), and
    - an approved review-gate approval targeting the run with actor 'auditor',
    both strictly AFTER the run's latest 'run.awaiting_review' event (equal
    timestamps do not count): an audit recorded before verification finished, e.g.
    by code running inside the run, is worthless.

    The actor name 'auditor' is a convention that tells `navigator audited` apart
    from the CLI `retry` grant (actor 'operator'); it is NOT authentication: anyone
    who can write to ACT can forge it. Fails closed: when the events cannot be listed
    (a client without list_events, ACT unreachable) the run is not audited.
    """
    if isinstance(run, str):
        run = _find_run(act, run)
    if run is None or not run.verified_sha:
        return False
    events = _events(act, since=_audit_since(run))
    if events is None:
        return False
    mine = [e for e in events if _payload(e).get("run_id") == run.id]
    reviewed = [t for e in mine if getattr(e, "kind", None) == "run.awaiting_review"
                if (t := _parse_ts(getattr(e, "ts", None))) is not None]
    if not reviewed:
        return False
    since = max(reviewed)
    sha_audited = any(getattr(e, "kind", None) == "run.audited"
                      and _payload(e).get("verified_sha") == run.verified_sha
                      and (t := _parse_ts(getattr(e, "ts", None))) is not None and t > since
                      for e in mine)
    if not sha_audited:
        return False
    grants = act.list_approvals(target=run.id, gate=ApprovalGate.review,
                                decision=ApprovalDecision.approved)
    return any(a.actor == AUDITOR
               and (t := _parse_ts(a.decided_at)) is not None and t > since
               for a in grants)


def merge_tier_of(act, run) -> str | None:
    """The merge tier recorded on the run's LATEST run.awaiting_review event payload
    ("merge_tier"), or None when the events cannot be listed, no such event exists or
    the value is not "A"/"B"/"C". `run` is a Run or an open run's id (same as
    is_audited). On an exact timestamp tie the last event in list order wins."""
    if isinstance(run, str):
        run = _find_run(act, run)
    if run is None:
        return None
    events = _events(act, since=_audit_since(run))
    if events is None:
        return None
    best, best_ts = None, None
    for e in events:
        if getattr(e, "kind", None) != "run.awaiting_review":
            continue
        payload = _payload(e)
        if payload.get("run_id") != run.id:
            continue
        ts = _parse_ts(getattr(e, "ts", None))
        if ts is None:
            continue
        if best_ts is None or ts >= best_ts:
            best, best_ts = payload, ts
    if best is None:
        return None
    tier = best.get("merge_tier")
    return tier if tier in ("A", "B", "C") else None


def _audited(act, args: list[str], out) -> int:
    if len(args) == 1:
        note = None
    elif len(args) == 3 and args[1] == "--note":
        note = args[2]
    else:
        print("usage: navigator audited <run-id> [--note TEXT]", file=out)
        return 2
    run_id = args[0]
    run = _find_run(act, run_id)
    if run is None:
        print(f"no open run: {run_id}", file=out)
        return 1
    if run.phase != RunPhase.awaiting_review:
        print(f"{run_id} is {run.phase.value}, not awaiting_review", file=out)
        return 1
    if not run.verified_sha:
        print(f"{run_id} has no verified_sha recorded; nothing to audit", file=out)
        return 1
    if is_audited(act, run):
        print(f"{run_id} is already marked audited", file=out)
        return 0
    # the audit is bound to the exact commit that was verified: a run re-verified at
    # another sha must be audited again
    act.emit_event(Event(kind="run.audited", correlation_id=run.correlation_id,
                         payload={"run_id": run_id, "note": note, "actor": AUDITOR,
                                  "verified_sha": run.verified_sha}))
    grant = act.create_approval(target=run_id, gate=ApprovalGate.review)
    act.decide_approval(grant.id, ApprovalDecision.approved, actor=AUDITOR)
    print(f"{run_id} marked audited at {run.verified_sha[:12]} "
          f"(telegram /merge {run_id} is now accepted)", file=out)
    return 0


# ---- inbox -------------------------------------------------------------------

def _inbox_path() -> Path:
    env = os.environ.get("NAVIGATOR_TELEGRAM_INBOX_PATH")
    if env:
        return Path(env)
    # imported lazily: telegram_inbox imports is_audited from this module
    from navigator.observability.telegram_inbox import DEFAULT_INBOX_PATH
    return Path(DEFAULT_INBOX_PATH)


def _inbox(args: list[str], out) -> int:
    """Print the Telegram inbox file as written by TelegramInbox: one JSON object per
    line, newest last. The rotated file <path>.1 (if present) is printed first, then
    <path>, so entries that were just rotated out are not lost. With --since only
    entries whose update_id is greater than UPDATE_ID (applied across both files).
    Lines that are not JSON (a partial write) are skipped; bytes that are not UTF-8
    are replaced, never fatal. A missing file prints nothing."""
    if args == []:
        since = None
    elif len(args) == 2 and args[0] == "--since" and args[1].lstrip("-").isdigit():
        since = int(args[1])
    else:
        print("usage: navigator inbox [--since UPDATE_ID]", file=out)
        return 2
    path = _inbox_path()
    for p in (Path(f"{path}.1"), path):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if since is not None:
                uid = entry.get("update_id") if isinstance(entry, dict) else None
                if not isinstance(uid, int) or isinstance(uid, bool) or uid <= since:
                    continue
            print(line, file=out)
    return 0


def _diff(act, run_id, out) -> int:
    run = _find_run(act, run_id)
    prop = act.get_proposal(run.proposal_id) if run else None
    if run is None or prop is None:
        print(f"no open run: {run_id}", file=out)
        return 1
    print(f"{run.id}  {prop.title}  (model calls: {run.model_calls})", file=out)
    print(f"verified: {run.verified_sha or '(not recorded)'}", file=out)
    print(format_verification(run.verification), file=out)
    d = subprocess.run(["git", "diff", f"{run.base_commit_sha}..{run.branch}"], cwd=prop.repo,
                       capture_output=True, text=True)
    out.write(d.stdout or d.stderr)
    return d.returncode


def _retry(act, pid, out) -> int:
    p = act.get_proposal(pid)
    if p is None:
        print(f"no such proposal: {pid}", file=out)
        return 1
    runs = act.list_runs_for_proposal(pid)
    if not runs or any(r.status in (RunStatus.executing, RunStatus.completed) for r in runs):
        print(f"{pid} has no failed run to retry (or one is open/completed)", file=out)
        return 1
    if p.status != ProposalStatus.approved:
        act.set_proposal_status(pid, ProposalStatus.approved)
    grant = act.create_approval(target=pid, gate=ApprovalGate.review)
    act.decide_approval(grant.id, ApprovalDecision.approved, actor=ACTOR)
    print(f"{pid}: one retry granted (runs on the daemon's next execute cycle)", file=out)
    return 0


# ---- settings (U2b: the UI document from the terminal) ------------------------

_SETTINGS_DOC = "navigator/settings"
_STATUS_DOC = "navigator/status"
_SETTINGS_USAGE = ("usage: navigator settings | navigator settings set <field> <value> | "
                   "navigator settings unset <field>")
# hot fields whose raw CLI value is always kept as a string (never parsed as a number)
_STRING_SETTINGS = frozenset({"sdk_model", "decisions_tz"})
_SDK_ROLE_ENV_KEYS = ("NAVIGATOR_SDK_MODEL_PLANNER", "NAVIGATOR_SDK_MODEL_BUILDER",
                      "NAVIGATOR_SDK_MODEL_VERIFIER")


def _json(value) -> str:
    """One-line JSON form of a document value, control characters replaced."""
    return _printable(json.dumps(value, sort_keys=True, default=str))


def _settings(act, args: list[str], out) -> int:
    if args == []:
        return _settings_show(act, out)
    if len(args) == 3 and args[0] == "set":
        return _settings_set(act, args[1], args[2], out)
    if len(args) == 2 and args[0] == "unset":
        return _settings_unset(act, args[1], out)
    print(_SETTINGS_USAGE, file=out)
    return 2


def _settings_show(act, out) -> int:
    """Print the settings document and what the daemon reports as in effect: one line per
    hot field, labelled (document) when the document sets it, (daemon env/default) when
    only the status document knows its value, (no status) otherwise. Read-only; an ACT
    error on either read prints one line and exits 1."""
    from navigator.config import HOT_FIELDS
    try:
        doc = act.get_doc(_SETTINGS_DOC)
        status = act.get_doc(_STATUS_DOC)
    except Exception as e:
        print(f"settings: ACT unreachable ({type(e).__name__})", file=out)
        return 1
    body = doc[0] if doc is not None and isinstance(doc[0], dict) else {}
    if doc is None:
        print("settings document: absent", file=out)
    else:
        print(f"settings document: version {_printable(doc[1])}", file=out)
    sbody = status[0] if status is not None and isinstance(status[0], dict) else None
    if sbody is None:
        print("daemon status: absent", file=out)
    else:
        print(f"daemon status: cycle {_printable(sbody.get('cycle'))} at "
              f"{_printable(sbody.get('ts'))}, applied version "
              f"{_printable(sbody.get('settings_version'))}", file=out)
    effective = sbody.get("effective") if sbody is not None else None
    if not isinstance(effective, dict):
        effective = {}
    for name in sorted(HOT_FIELDS):
        if name in body:
            print(f"{name} = {_json(body[name])}  (document)", file=out)
        elif name in effective:
            print(f"{name} = {_json(effective[name])}  (daemon env/default)", file=out)
        else:
            print(f"{name} = ?  (no status)", file=out)
    if sbody is not None:
        rejected = sbody.get("settings_rejected")
        if isinstance(rejected, list) and rejected:
            print("rejected: " + ", ".join(_printable(r) for r in rejected), file=out)
        if sbody.get("settings_error"):
            print(f"settings error: {_printable(sbody['settings_error'])}", file=out)
    return 0


def _parse_setting(name: str, raw: str):
    """CLI text -> document value. sdk_model and decisions_tz stay strings; sdk_role_models
    takes 'planner=m1,builder=m2' (empty = {}; a pair without '=' is a ValueError);
    elsewhere 'true'/'false' -> bool, an int literal -> int, a FINITE float literal ->
    float, anything else (including inf/nan) stays a string for Settings.overlay to
    refuse."""
    if name in _STRING_SETTINGS:
        return raw
    if name == "sdk_role_models":
        pairs: dict[str, str] = {}
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            role, sep, model = chunk.partition("=")
            if not sep:
                raise ValueError("expected role=model pairs")
            pairs[role.strip()] = model.strip()
        return pairs
    text = raw.strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return raw
    return number if math.isfinite(number) else raw


def _settings_set(act, name: str, raw: str, out) -> int:
    """Validate with Settings.overlay (env-only/unknown fields and bad values are refused,
    exit 2), then write the document with the version just read as expected_version."""
    from navigator.clients.act_client import DocVersionConflict
    from navigator.config import Settings
    try:
        value = _parse_setting(name, raw)
    except ValueError as e:
        print(f"refused: {_printable(name)}: {e}", file=out)
        return 2
    _, rejected = Settings().overlay({name: value})
    if rejected:
        print(f"refused: {_printable(rejected[0])}", file=out)
        return 2
    try:
        doc = act.get_doc(_SETTINGS_DOC)
        body, expected = (dict(doc[0]), doc[1]) if doc is not None else ({}, 0)
        body[name] = value
        version = act.put_doc(_SETTINGS_DOC, body, actor=ACTOR, expected_version=expected)
    except DocVersionConflict as e:
        print(f"settings changed meanwhile (now version {e.current_version}) - run it again",
              file=out)
        return 1
    except Exception as e:
        print(f"settings: ACT unreachable ({type(e).__name__})", file=out)
        return 1
    print(f"{_SETTINGS_DOC} version {version}: {name} = {_json(value)} "
          f"(the daemon applies it at its next cycle)", file=out)
    return 0


def _settings_unset(act, name: str, out) -> int:
    """Remove one key from the document (expected_version = the version just read). A key
    that is not set writes nothing and exits 0."""
    from navigator.clients.act_client import DocVersionConflict
    try:
        doc = act.get_doc(_SETTINGS_DOC)
        if doc is None or not isinstance(doc[0], dict) or name not in doc[0]:
            print(f"{_printable(name)} is not set in the document", file=out)
            return 0
        body = dict(doc[0])
        del body[name]
        version = act.put_doc(_SETTINGS_DOC, body, actor=ACTOR, expected_version=doc[1])
    except DocVersionConflict as e:
        print(f"settings changed meanwhile (now version {e.current_version}) - run it again",
              file=out)
        return 1
    except Exception as e:
        print(f"settings: ACT unreachable ({type(e).__name__})", file=out)
        return 1
    print(f"{_SETTINGS_DOC} version {version}: {_printable(name)} unset "
          f"(the daemon applies it at its next cycle)", file=out)
    return 0


def _settings_env_keys(name: str) -> tuple[str, ...]:
    """The plist env key(s) the daemon would read for hot field `name`."""
    if name == "sdk_role_models":
        return _SDK_ROLE_ENV_KEYS
    return ("NAVIGATOR_" + name.upper(),)


def _check_settings_doc(act, env: dict, out) -> None:
    """doctor: report the settings document (read-only, informational): absent, unreadable
    (exception class only) or present with its version; one WARN per plist env key the
    document overrides; one WARN per key the daemon refused (navigator/status
    settings_rejected). Never affects doctor's exit code; never writes."""
    if act is None:
        return
    from navigator.config import HOT_FIELDS
    try:
        doc = act.get_doc(_SETTINGS_DOC)
    except Exception as e:
        print(f"WARN settings document unreadable: {type(e).__name__}", file=out)
        return
    if doc is None:
        print("info settings document absent (daemon runs on its environment)", file=out)
    else:
        body, version = doc
        print(f"ok   settings document version {_printable(version)}", file=out)
        body = body if isinstance(body, dict) else {}
        for name in sorted(HOT_FIELDS):
            if name not in body:
                continue
            for key in _settings_env_keys(name):
                if key in env:
                    print(f"WARN the settings document overrides {key} (plist value ignored)",
                          file=out)
    try:
        status = act.get_doc(_STATUS_DOC)
    except Exception as e:
        print(f"WARN daemon status unreadable: {type(e).__name__}", file=out)
        return
    if status is not None and isinstance(status[0], dict):
        rejected = status[0].get("settings_rejected")
        for item in rejected if isinstance(rejected, list) else []:
            print(f"WARN settings document key refused: {_printable(item)}", file=out)


# ---- doctor ------------------------------------------------------------------

_DEFAULT_PLIST = "~/Library/LaunchAgents/com.calwbot.navigator.plist"
_MIN_SDK = (0, 2, 165)
_SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASS", "CREDENTIAL", "AUTH", "PRIVATE",
                      "COOKIE", "SESSION", "BEARER", "DSN", "CERT", "SIGNATURE")
# drivers _make_driver accepts
_DRIVERS = ("fake", "claude_sdk")
# launchd's working directory when the plist sets none
_LAUNCHD_DEFAULT_CWD = "/"
_REDACTED = "<redacted>"
_URL_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:/")
_AUTHORITY_END = re.compile(r"[/?#\s]")
_HOST_PORT = re.compile(r"^(?:[A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?$")
# control, format, surrogate and line/paragraph separator characters
_UNPRINTABLE_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")


def _printable(value) -> str:
    """`value` with every control/format/line-separator character replaced by '?',
    so printing it can never start a new output line."""
    return "".join("?" if unicodedata.category(ch) in _UNPRINTABLE_CATEGORIES else ch
                   for ch in str(value))


def _safe(value) -> str:
    """Printable form of an env-derived value that cannot leak secrets.

    URL with scheme -> scheme://host[:port] plus '/...' if anything followed the
    authority (userinfo, path, query and fragment are dropped). Anything ambiguous
    (an '@' after the authority, a host[:port] that does not parse) -> '<redacted>'.
    Non-URL values containing '@', '%40', '?' or '://', or starting with
    'scheme:/' -> '<redacted>'. Everything else is returned unchanged except that
    control characters (and line/paragraph separators) become '?'.
    """
    value = str(value)
    m = _URL_SCHEME.match(value)
    if m:
        scheme, rest = m.group(1), m.group(2)
        end = _AUTHORITY_END.search(rest)
        authority = rest[:end.start()] if end else rest
        tail = rest[len(authority):]
        if "@" in tail:
            # userinfo may itself contain '/', '?', '#' or spaces: can't tell where it ends
            return _REDACTED
        host = authority.rsplit("@", 1)[1] if "@" in authority else authority
        if not _HOST_PORT.match(host):
            return _REDACTED
        return _printable(f"{scheme}://{host}" + ("/..." if tail else ""))
    if ("@" in value or "%40" in value or "?" in value or "://" in value
            or _SCHEME_PREFIX.match(value)):
        return _REDACTED
    return _printable(value)


def _redact(key: str, value: str) -> str:
    upper = key.upper()
    if any(m in upper for m in _SENSITIVE_MARKERS):
        return _REDACTED
    if upper.startswith("NAVIGATOR_") or upper == "PATH":
        return _safe(value)
    return _REDACTED


def _sdk_version() -> str | None:
    """Installed claude-agent-sdk version, or None if not installed."""
    from importlib import metadata
    try:
        return metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return None


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for piece in v.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            raise ValueError("unparseable version")
        parts.append(int(digits))
    return tuple(parts)


def _settings_with_env(env: dict[str, str], cwd: str):
    """Run the daemon's Settings.from_env + effective_repos with os.environ replaced by
    ONLY `env` and the process cwd set to `cwd`; both are restored afterwards. The
    environment is restored first and independently, so a failing chdir back cannot
    leave the process running with the plist's environment."""
    from navigator.config import Settings, effective_repos
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(cwd)
        settings = Settings.from_env()
        return settings, effective_repos(settings)
    finally:
        try:
            os.environ.clear()
            os.environ.update(saved_env)
        finally:
            os.chdir(saved_cwd)


# pending merge approvals older than this are reported as stale
_DEFAULT_STALE_HOURS = 24.0
_STALE_ENV = "NAVIGATOR_STALE_APPROVAL_HOURS"
_STALE_PREFIX = "WARN stale merge approval "


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _approval_created_at(approval) -> datetime | None:
    """When the approval was created: an explicit created_at if ACT ever records one,
    else the timestamp encoded in its ULID id ('approval-<ULID>'); None if neither
    is usable."""
    created = _parse_ts(getattr(approval, "created_at", None))
    if created is not None:
        return created
    try:
        raw = str(getattr(approval, "id", "")).rsplit("-", 1)[-1]
        return _parse_ts(ULID.from_str(raw).datetime)
    except Exception:
        return None


def _stale_threshold(out) -> timedelta:
    """NAVIGATOR_STALE_APPROVAL_HOURS from the shell env as a timedelta; a missing or
    empty value means the default, an unusable one prints a WARN and uses the default."""
    raw = os.environ.get(_STALE_ENV)
    if raw is None or raw.strip() == "":
        return timedelta(hours=_DEFAULT_STALE_HOURS)
    try:
        hours = float(raw)
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("not a positive finite number")
        return timedelta(hours=hours)
    except (ValueError, OverflowError):
        print(f"WARN {_STALE_ENV}: not a positive number, using {_DEFAULT_STALE_HOURS:g}h",
              file=out)
        return timedelta(hours=_DEFAULT_STALE_HOURS)


def _check_stale_approvals(act, out) -> None:
    """Print one WARN line per pending merge approval (of an open run) that is older
    than the stale threshold. Read-only; a client failure prints a WARN with the
    exception class name only. Does not affect doctor's exit code."""
    if act is None:
        print("skip stale merge approvals (no ACT client)", file=out)
        return
    threshold = _stale_threshold(out)
    hours = threshold.total_seconds() / 3600
    now = _now()
    stale = []
    try:
        runs = list(act.list_open_runs())
        for run in runs:
            for a in act.list_approvals(target=run.id, gate=ApprovalGate.merge,
                                        decision=ApprovalDecision.pending):
                created = _approval_created_at(a)
                if created is None:
                    continue
                age = now - created
                if age > threshold:
                    stale.append((run, a, age))
    except Exception as e:
        print(f"WARN stale merge approvals unreadable: {type(e).__name__}", file=out)
        return
    for run, a, age in stale:
        phase = getattr(getattr(run, "phase", None), "value", "?")
        print(f"{_STALE_PREFIX}{_printable(a.id)} for {_printable(run.id)}: pending "
              f"{age.total_seconds() / 3600:.1f}h > {hours:g}h (phase={_printable(phase)}; "
              f"forgotten, or invalidated by a rebase since)", file=out)
    if not stale:
        print(f"ok   no pending merge approval older than {hours:g}h "
              f"({len(runs)} open run(s))", file=out)


# the daemon's planner backpressure (runtime/loop.py periodic_tick), mirrored read-only
_BACKLOG_PREFIX = "WARN backlog "
# launchd plists that each poll Telegram's getUpdates; Telegram allows one reader
_DUPLICATE_READERS_PREFIX = "WARN duplicate telegram readers"
_LEGACY_READER_PLIST = "com.calwbot.navigator-telegram.plist"
_ONEBOT_PLIST = "com.calwbot.telegram-bot.plist"


def _check_backlog(act, settings, repos, out) -> None:
    """Per configured repo, print whether the daemon's open-proposal backlog has
    reached settings.max_open_proposals, counted exactly like periodic_tick: only
    that repo's proposed proposals, those older than proposal_ttl_days ignored.
    Read-only: nothing is expired, no proposal status is changed. Only counts when
    this shell's ACT is the daemon's (NAVIGATOR_ACT_BASE_URL from the plist). A
    client failure prints a WARN with the exception class name only. Never affects
    doctor's exit code."""
    if act is None:
        print("skip backlog (no ACT client)", file=out)
        return
    if settings is None or repos is None:
        print("skip backlog (settings unavailable)", file=out)
        return
    if not settings.act_base_url:
        print("skip backlog (daemon uses an in-memory ACT: NAVIGATOR_ACT_BASE_URL unset "
              "in plist)", file=out)
        return
    client_url = getattr(act, "base_url", None)
    expected = f"{settings.act_base_url.rstrip('/')}/api/v1/agent"
    if client_url is not None and client_url != expected:
        print(f"WARN backlog: this shell's ACT ({_safe(client_url)}) is not the daemon's "
              f"({_safe(expected)}); not counted", file=out)
        return
    try:
        proposals = list(act.list_proposals(ProposalStatus.proposed))
    except Exception as e:
        print(f"WARN backlog unreadable: {type(e).__name__}", file=out)
        return
    cutoff = _now() - timedelta(days=settings.proposal_ttl_days)
    cap = settings.max_open_proposals
    for r in repos:
        path = str(getattr(r, "path", r))
        live = [p for p in proposals if p.repo == path
                and (created := _parse_ts(getattr(p, "created_at", None))) is not None
                and created >= cutoff]
        n = len(live)
        if n >= cap:
            print(f"{_BACKLOG_PREFIX}{_safe(path)}: {n} open proposals >= cap {cap}; "
                  f"planner paused until you triage", file=out)
        else:
            print(f"ok   backlog {_safe(path)}: {n}/{cap} open", file=out)


def _launch_agents_dir() -> Path:
    # evaluated at call time so tests can redirect it; never at import
    return Path.home() / "Library" / "LaunchAgents"


def _check_duplicate_telegram_readers(out, launch_agents_dir: Path | None = None) -> None:
    """Warn when both the legacy navigator-telegram plist and the one-bot
    telegram-bot plist are installed (exact file names only; a renamed
    '<name>.disabled-<date>' does not count). File presence only: no launchctl, no
    subprocess, nothing written. Never affects doctor's exit code."""
    d = launch_agents_dir if launch_agents_dir is not None else _launch_agents_dir()
    try:
        if not d.is_dir():
            print(f"ok   telegram readers: no LaunchAgents dir ({_printable(d)})", file=out)
            return
        legacy = (d / _LEGACY_READER_PLIST).exists()
        onebot = (d / _ONEBOT_PLIST).exists()
    except Exception as e:
        print(f"WARN telegram readers unreadable: {type(e).__name__}", file=out)
        return
    if legacy and onebot:
        # the uid comes from the running process: launchd domains are per user
        print(f"{_DUPLICATE_READERS_PREFIX}: both {_LEGACY_READER_PLIST} and {_ONEBOT_PLIST} "
              f"are in {_printable(d)}; Telegram allows one getUpdates reader. Fix: "
              f"launchctl bootout gui/{os.getuid()}/com.calwbot.navigator-telegram && "
              f"mv the legacy plist to <name>.disabled-<date>", file=out)
    elif legacy or onebot:
        name = _LEGACY_READER_PLIST if legacy else _ONEBOT_PLIST
        print(f"ok   telegram reader {name} only", file=out)
    else:
        print("ok   telegram readers: none installed", file=out)


def _doctor(args: list[str], out, act=None) -> int:
    if args == []:
        plist_arg = _DEFAULT_PLIST
    elif len(args) == 2 and args[0] == "--plist":
        plist_arg = args[1]
    else:
        print("usage: navigator doctor [--plist PATH]", file=out)
        return 2
    plist = Path(plist_arg).expanduser()
    shown_plist = _printable(plist)
    failures = 0

    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except Exception as e:
        print(f"FAIL plist: {type(e).__name__} {shown_plist}", file=out)
        return 1
    if not isinstance(data, dict):
        print(f"FAIL plist: top level is not a dict {shown_plist}", file=out)
        return 1
    env_raw = data.get("EnvironmentVariables", {})
    if not isinstance(env_raw, dict):
        print(f"FAIL plist: EnvironmentVariables is not a dict {shown_plist}", file=out)
        return 1

    env: dict[str, str] = {}
    for k, v in env_raw.items():
        key = str(k)
        if not isinstance(v, str):
            print(f"WARN env {_printable(key)}: non-string value ({type(v).__name__}) "
                  f"coerced with str()", file=out)
        env[key] = str(v)

    print(f"plist: {shown_plist}", file=out)
    print(f"EnvironmentVariables ({len(env)}):", file=out)
    for key in sorted(env):
        print(f"  {_printable(key)}={_redact(key, env[key])}", file=out)

    # working directory: the daemon resolves relative paths against it
    wd_raw = data.get("WorkingDirectory", _LAUNCHD_DEFAULT_CWD)
    wd: str | None = None
    if not isinstance(wd_raw, str):
        failures += 1
        print(f"FAIL workingdir: not a string ({type(wd_raw).__name__})", file=out)
    elif not Path(wd_raw).is_absolute():
        failures += 1
        print(f"FAIL workingdir: not an absolute path {_safe(wd_raw)}", file=out)
    elif not Path(wd_raw).is_dir():
        failures += 1
        print(f"FAIL workingdir missing: {_safe(wd_raw)}", file=out)
    else:
        wd = wd_raw
        print(f"working directory: {_safe(wd)}", file=out)

    # settings + repos: the daemon's own startup, seeing only the plist env and cwd
    settings = None
    repos = None
    if wd is not None:
        try:
            settings, repos = _settings_with_env(env, wd)
        except Exception as e:
            failures += 1
            print(f"FAIL settings: {type(e).__name__}", file=out)
            settings = repos = None

    if settings is not None:
        driver = settings.driver
        if driver not in _DRIVERS:
            failures += 1
            print(f"FAIL driver: unknown NAVIGATOR_DRIVER {_safe(driver)!r}", file=out)
        else:
            print(f"ok   driver {driver}", file=out)
    else:
        driver = env.get("NAVIGATOR_DRIVER", "fake")

    if repos is not None:
        repos = list(repos)
        if not repos:
            failures += 1
            print("FAIL repos: no repos configured", file=out)
        for r in repos:
            try:
                path = str(getattr(r, "path", r))
                shown = _safe(path)
                p = Path(path)  # deliberately no expanduser: the daemon doesn't expand
                if not p.is_absolute():
                    p = Path(wd) / p
                if not p.is_dir():
                    failures += 1
                    print(f"FAIL repo missing: {shown}", file=out)
                elif not (p / ".git").exists():
                    failures += 1
                    print(f"FAIL repo not a git repository: {shown}", file=out)
                else:
                    print(f"ok   repo {shown}", file=out)
            except Exception as e:
                failures += 1
                print(f"FAIL repo: {type(e).__name__}", file=out)

    # SDK version: only the claude_sdk driver needs it
    if driver == "fake":
        print("skip claude-agent-sdk (driver=fake)", file=out)
    elif driver == "claude_sdk":
        try:
            ver = _sdk_version()
            if ver is None:
                failures += 1
                print("FAIL claude-agent-sdk not installed", file=out)
            elif _version_tuple(ver) < _MIN_SDK:
                failures += 1
                print(f"FAIL claude-agent-sdk {_printable(ver)} < "
                      f"{'.'.join(map(str, _MIN_SDK))}", file=out)
            else:
                print(f"ok   claude-agent-sdk {_printable(ver)}", file=out)
        except Exception as e:
            failures += 1
            print(f"FAIL claude-agent-sdk version: {type(e).__name__}", file=out)

    # navigator code HEAD (informational)
    nav_root = Path(__file__).resolve().parent.parent
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=nav_root,
                              capture_output=True, text=True)
        if head.returncode == 0 and head.stdout.strip():
            print(f"navigator HEAD {head.stdout.strip()}", file=out)
        else:
            print(f"WARN navigator HEAD unknown ({nav_root})", file=out)
    except Exception as e:
        print(f"WARN navigator HEAD: {type(e).__name__} {nav_root}", file=out)

    # pending merge approvals that have waited too long (informational, read-only)
    _check_stale_approvals(act, out)
    # planner backpressure per repo, counted like the daemon (read-only)
    _check_backlog(act, settings, repos, out)
    # two launchd jobs polling Telegram's getUpdates (file presence only)
    _check_duplicate_telegram_readers(out)
    # U2b: the settings document vs the plist env (informational, never a failure)
    _check_settings_doc(act, env, out)

    if failures:
        print(f"doctor: {failures} problem(s)", file=out)
        return 1
    print("doctor: ok", file=out)
    return 0


# ---- cleanup -----------------------------------------------------------------

def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _resolve_worktree_root(worktree_root: str | None) -> str | None:
    if worktree_root:
        return worktree_root
    env = os.environ.get("NAVIGATOR_WORKTREE_ROOT")
    if env:
        return env
    try:
        from navigator.config import Settings
        return Settings().worktree_root or None
    except Exception:
        return None


def _eligible(run, now: datetime) -> bool:
    status = run.status.value
    if status in _FINISHED_STATUSES:
        return True
    if status == "failed" and run.ended_at is not None:
        ended = run.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        return now - ended > _FAILED_GRACE
    return False


def _protected_branches(repo) -> set[str]:
    protected = set(_ALWAYS_PROTECTED)
    cur = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if cur.returncode == 0 and cur.stdout.strip():
        protected.add(cur.stdout.strip())
    return protected


def _cleanup_run(act, run, *, root: Path, apply: bool, prefix: str, out,
                 touched: list[str], protected_by_repo: dict[str, set[str]]) -> int:
    """Clean one run; returns the number of git command failures."""
    failures = 0
    prop = act.get_proposal(run.proposal_id)
    if prop is None or not prop.repo or not Path(prop.repo).is_dir():
        print(f"skip {run.id}: repo unknown or missing", file=out)
        return 0
    repo = prop.repo

    def _mark_touched():
        if repo not in touched:
            touched.append(repo)

    wt = Path(run.worktree_path) if run.worktree_path else root / run.id
    wt = wt.expanduser().resolve()
    if wt.exists() and root in wt.parents:
        _mark_touched()
        print(f"{prefix}remove worktree {wt} ({run.id})", file=out)
        if apply:
            r = _git(repo, "worktree", "remove", "--force", str(wt))
            if r.returncode != 0:
                failures += 1
                print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)

    branch = f"nav/{run.id}"
    if repo not in protected_by_repo:
        protected_by_repo[repo] = _protected_branches(repo)
    if (branch.startswith(_BRANCH_PREFIX) and branch not in protected_by_repo[repo]
            and _git(repo, "rev-parse", "--verify", "--quiet",
                     f"refs/heads/{branch}").returncode == 0):
        _mark_touched()
        print(f"{prefix}delete branch {branch} in {repo} ({run.id})", file=out)
        if apply:
            r = _git(repo, "branch", "-D", branch)
            if r.returncode != 0:
                failures += 1
                print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)
    return failures


def _cleanup(act, *, apply: bool, out, worktree_root: str | None = None,
             now: datetime | None = None) -> int:
    root_s = _resolve_worktree_root(worktree_root)
    if not root_s:
        print("cleanup needs NAVIGATOR_WORKTREE_ROOT to be set", file=out)
        return 1
    root = Path(root_s).expanduser().resolve()
    now = now or datetime.now(timezone.utc)
    prefix = "" if apply else "would "
    touched: list[str] = []
    protected_by_repo: dict[str, set[str]] = {}
    failures = 0
    errors = 0

    for run in act.list_all_runs():
        if run.status == RunStatus.executing or not _eligible(run, now):
            continue
        try:
            failures += _cleanup_run(act, run, root=root, apply=apply, prefix=prefix, out=out,
                                     touched=touched, protected_by_repo=protected_by_repo)
        except Exception as e:
            errors += 1
            print(f"error {run.id}: {type(e).__name__}", file=out)

    for repo in touched:
        print(f"{prefix}prune worktrees in {repo}", file=out)
        if apply:
            r = _git(repo, "worktree", "prune")
            if r.returncode != 0:
                failures += 1
                print(f"  failed: {(r.stderr or r.stdout).strip()}", file=out)
    if not touched:
        print("nothing to clean up", file=out)
    if errors:
        print(f"cleanup finished with {errors} errored run(s)", file=out)
    return 1 if (failures or errors) else 0


_HANDLERS = {
    "approve": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.approved),
    "decline": lambda act, i, out: _set_proposal(act, i, out, ProposalStatus.declined),
    "merge": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.approved),
    "reject": lambda act, i, out: _decide_merge(act, i, out, ApprovalDecision.declined),
    "diff": _diff,
    "retry": _retry,
}
