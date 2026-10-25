"""Actions v1.1 (operator 2026-10-24, follow-up 2026-10-25): one pinned script per manual
step, run from Telegram.

Operator-only steps (launchctl bootstrap of the backup and caffeinate plists, ...) need
a command typed at the Mac: auto mode denies Claude those actions and the items sat in
the decisions inbox since 2026-10-23. The operator approved phone-approved SCOPED
actions: a manual item (decisions.py) may carry ONE pinned script - the absolute path
plus the sha256 recorded when it was added - and the operator runs it by sending
'run <id>' in Telegram ('show <id>' prints the script first). Nothing else is runnable.

Safety rules, all enforced here:
  only the pinned path is ever executed (argv = [script], shell=False); nothing typed
    in the message reaches the script and there are no arguments; the path must be
    absolute (a relative one would be searched on PATH)
  the script must live directly in the actions dir, be a regular file (no symlink)
    owned by the current user, executable and not group/world-writable; the dir itself
    must be owned by the user and not group/world-writable (check_script); 'show' runs
    the same checks before reading the file and warns when the sha256 no longer matches
  a script whose sha256 differs from the pinned one is refused: edit = re-approve
  one run at a time (flock on <actions dir>/.run.lock), at most one run per item per
    MIN_INTERVAL_S, RUN_TIMEOUT_S of wall clock, a minimal environment (PATH=SAFE_PATH
    and HOME only), stdin closed, cwd = the actions dir
  a 'started' line is written to the action log under the lock BEFORE the script runs
    and the run is refused when it cannot be written (no record = no run); the final
    'ran'/'timeout' line is written before the lock is released, so a crash mid-run
    still leaves a 'started' record, and that record counts for the rate limit
  the script runs in its own session (start_new_session) and on timeout the WHOLE
    process group is killed; the pipes are closed after a bounded reap, so a process
    that escaped the group (setsid) cannot hang the bot
  every attempt - run or refused - is one JSON line in the action log (created 0o600);
    the script's output goes to the Telegram reply only (the last MAX_OUTPUT_CHARS),
    never to the log; replies and log lines carry fixed reasons or exception class
    names only, never exception text; non-UTF-8 output is replaced, never raised on
  a run never removes the manual item: the operator sends 'done <id>' (the decisions
    CLI) once the step is really finished

The bot calls run_action synchronously inside its loop, so one run blocks the bot for at
most RUN_TIMEOUT_S plus the bounded reap (REAP_TIMEOUT_S). decisions.py imports this
module (add_manual pins through check_script/sha256_file); decisions is imported lazily
here.
"""
import fcntl
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import time

from navigator.observability.logging import get_logger

log = get_logger("navigator.actions")

DEFAULT_ACTIONS_DIR = "/Users/Manoj/calwbot/logs/navigator/tools/actions"
DEFAULT_ACTION_LOG = "/Users/Manoj/calwbot/logs/navigator/action_runs.jsonl"
RUN_TIMEOUT_S = 60
MIN_INTERVAL_S = 60
MAX_OUTPUT_CHARS = 1500
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LOCK_NAME = ".run.lock"
REAP_TIMEOUT_S = 5  # bounded wait for the pipes after the process group was killed


def actions_dir() -> str:
    return os.getenv("NAVIGATOR_ACTIONS_DIR") or DEFAULT_ACTIONS_DIR


def action_log_path() -> str:
    return os.getenv("NAVIGATOR_ACTION_LOG_PATH") or DEFAULT_ACTION_LOG


def _dir_or_default(value) -> str:
    return value or actions_dir()


# ---- what may be pinned / run ---------------------------------------------------

def check_script(path, actions_dir) -> str | None:
    """None when `path` may be pinned and run from `actions_dir`, else one fixed reason:
    'actions dir unsafe', 'missing', 'symlink', 'not a regular file', 'outside actions
    dir', 'not owned by you', 'group/world-writable', 'not executable'. The dir is
    checked first: a writable dir lets anyone swap the file under its name."""
    uid = os.getuid()
    try:
        d = os.stat(actions_dir)
        if d.st_uid != uid or d.st_mode & 0o022:
            return "actions dir unsafe"
        st = os.lstat(path)
    except OSError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(actions_dir):
        return "outside actions dir"
    if st.st_uid != uid:
        return "not owned by you"
    if st.st_mode & 0o022:
        return "group/world-writable"
    if not st.st_mode & 0o100:
        return "not executable"
    return None


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- the run log (one JSON line per attempt, never the output) -----------------

def _recent_run(log_path, item_id, now) -> bool:
    """True when the log holds a 'started'/'ran'/'timeout' line for item_id with a
    finite ts in (now - MIN_INTERVAL_S, now]; an unreadable or garbled log, a corrupt
    byte, a future ts or an infinite/NaN ts (json.loads accepts the Infinity/NaN
    tokens) never blocks."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("id") != item_id:
            continue
        if rec.get("outcome") not in ("started", "ran", "timeout"):
            continue
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        try:
            if not math.isfinite(ts):
                continue
        except OverflowError:
            continue
        if ts <= now and ts > now - MIN_INTERVAL_S:
            return True
    return False


def _append_log(log_path, record: dict) -> bool:
    """Append one JSON line; the file is created 0o600 (parents made). True on success;
    a failure is logged by class name only, never raises, and returns False."""
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        log.warning("actions.log_failed", error=type(e).__name__)
        return False
    return True


def _item(item_id, manual_file):
    from navigator.observability import decisions  # decisions imports this module
    for it in decisions.read_manual(manual_file or decisions.manual_path()):
        if it["id"] == item_id:
            return it
    return None


# ---- run / show -------------------------------------------------------------------

def _kill_group(proc) -> None:
    """After a timeout: SIGKILL the script's whole process group, then reap with a
    bounded wait. A process that left the group (setsid) may still hold the pipes:
    after REAP_TIMEOUT_S the pipes are closed and only the (already killed) direct
    child is waited for. Never an unbounded communicate()/wait() while the pipes are
    open."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        proc.wait()  # the direct child is SIGKILLed: this returns


def run_action(item_id, update_id=0, manual_file=None, actions_dir=None, log_path=None,
               now=None) -> str:
    """Run the pinned script of manual item `item_id` and return the Telegram reply.
    Refused WITHOUT executing (logged as 'refused' with the reason) when the item or
    its script is missing, the path is not absolute, check_script objects, the sha256
    differs from the pinned one, the item ran (or started) less than MIN_INTERVAL_S ago
    or another action holds the lock; 'not run: action log unwritable' when the
    'started' line cannot be written (nothing can be logged then). Otherwise the reply
    is 'ran <id>: rc=<rc>' plus the last MAX_OUTPUT_CHARS of the output, or
    'ran <id>: timed out after <N>s'. The manual item is never removed."""
    now = time.time() if now is None else float(now)
    adir = _dir_or_default(actions_dir)
    lpath = log_path or action_log_path()
    item = _item(item_id, manual_file)
    pinned = item.get("sha256") if item else None
    rec = {"ts": now, "id": item_id, "sha256": pinned, "update_id": update_id,
           "outcome": "refused", "rc": None, "reason": None}

    def refused(reason: str, reply: str) -> str:
        rec["reason"] = reason
        _append_log(lpath, rec)
        return reply

    if item is None:
        return refused("no such item", f"no manual step {item_id}")
    script = item.get("script")
    if not script or not pinned:
        return refused("no script", f"no script - do it yourself: {item['how']}")
    if not os.path.isabs(script):
        return refused("not absolute", "not run: not absolute")
    reason = check_script(script, adir)
    if reason is not None:
        return refused(reason, f"not run: {reason}")
    try:
        digest = sha256_file(script)
    except OSError:
        return refused("missing", "not run: missing")
    if digest != pinned:
        return refused("script changed", "not run: script changed since it was approved")
    if _recent_run(lpath, item_id, now):
        return refused("ran recently", f"not run: ran less than {MIN_INTERVAL_S}s ago")
    try:
        lock_fd = os.open(os.path.join(adir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        return refused(type(e).__name__, f"not run: {type(e).__name__}")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return refused("another action is running", "not run: another action is running")
        # the record that survives a crash mid-run: written before exec, and required
        if not _append_log(lpath, dict(rec, outcome="started", rc=None, reason=None)):
            return "not run: action log unwritable"
        timeout = RUN_TIMEOUT_S
        try:
            proc = subprocess.Popen(
                [script], shell=False, cwd=adir,
                env={"PATH": SAFE_PATH, "HOME": os.environ.get("HOME", "")},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            return refused(type(e).__name__, f"not run: {type(e).__name__}")
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            rec["outcome"] = "timeout"
            _append_log(lpath, rec)
            return f"ran {item_id}: timed out after {timeout}s"
        rec.update(outcome="ran", rc=proc.returncode)
        _append_log(lpath, rec)  # the final line is written while the lock is still held
    finally:
        os.close(lock_fd)  # releases the flock
    reply = f"ran {item_id}: rc={proc.returncode}"
    text = ((out or b"").decode("utf-8", errors="replace")
            + (err or b"").decode("utf-8", errors="replace")).rstrip()
    if text:
        reply += "\n" + text[-MAX_OUTPUT_CHARS:]
    return reply


def show_action(item_id, manual_file=None) -> str:
    """The pinned script of a manual item, for reading before 'run <id>': title, path,
    the first 12 hex of the pinned sha256, a WARNING line when the file no longer
    matches the pin, then the script text (capped at MAX_OUTPUT_CHARS, '...' appended
    when cut). The path is checked like 'run' does (absolute, check_script) before the
    file is opened: 'not shown: <reason>'."""
    item = _item(item_id, manual_file)
    if item is None:
        return f"no manual step {item_id}"
    script = item.get("script")
    if not script:
        return f"no script - do it yourself: {item['how']}"
    if not os.path.isabs(script):
        return "not shown: not absolute"
    reason = check_script(script, actions_dir())
    if reason is not None:
        return f"not shown: {reason}"
    pinned = item.get("sha256") or ""
    try:
        with open(script, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_OUTPUT_CHARS + 1)
    except OSError as e:
        return f"script unreadable: {type(e).__name__}"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "..."
    try:
        current = sha256_file(script)
    except OSError:
        current = None
    lines = [item["title"], f"script: {script}", f"pinned sha256: {pinned[:12]}"]
    if current != pinned:
        lines.append("WARNING: changed since approval - 'run' will refuse it")
    lines.append(text)
    return "\n".join(lines)
