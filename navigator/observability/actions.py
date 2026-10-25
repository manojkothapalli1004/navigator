"""Actions v1 (operator 2026-10-24): one pinned script per manual step, run from Telegram.

Operator-only steps (launchctl bootstrap of the backup and caffeinate plists, ...) need
a command typed at the Mac: auto mode denies Claude those actions and the items sat in
the decisions inbox since 2026-10-23. The operator approved phone-approved SCOPED
actions: a manual item (decisions.py) may carry ONE pinned script - the absolute path
plus the sha256 recorded when it was added - and the operator runs it by sending
'run <id>' in Telegram ('show <id>' prints the script first). Nothing else is runnable.

Safety rules, all enforced here:
  only the pinned path is ever executed (argv = [script], shell=False); nothing typed
    in the message reaches the script and there are no arguments
  the script must live directly in the actions dir, be a regular file (no symlink)
    owned by the current user, executable and not group/world-writable; the dir itself
    must be owned by the user and not group/world-writable (check_script)
  a script whose sha256 differs from the pinned one is refused: edit = re-approve
  one run at a time (flock on <actions dir>/.run.lock), at most one run per item per
    MIN_INTERVAL_S, RUN_TIMEOUT_S of wall clock, a minimal environment (PATH=SAFE_PATH
    and HOME only), stdin closed, cwd = the actions dir
  every attempt - run or refused - is one JSON line in the action log (created 0o600);
    the script's output goes to the Telegram reply only (the last MAX_OUTPUT_CHARS),
    never to the log; replies and log lines carry fixed reasons or exception class
    names only, never exception text
  a run never removes the manual item: the operator sends 'done <id>' (the decisions
    CLI) once the step is really finished

The bot calls run_action synchronously inside its loop, so one run blocks the bot for at
most RUN_TIMEOUT_S. decisions.py imports this module (add_manual pins through
check_script/sha256_file); decisions is imported lazily here.
"""
import fcntl
import hashlib
import json
import os
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
    """True when the log holds a 'ran'/'timeout' line for item_id newer than
    MIN_INTERVAL_S; an unreadable or garbled log counts as no recent run."""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
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
        if rec.get("outcome") not in ("ran", "timeout"):
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and ts > now - MIN_INTERVAL_S:
            return True
    return False


def _append_log(log_path, record: dict) -> None:
    """Append one JSON line; the file is created 0o600 (parents made). A failure is
    logged by class name only and never raises."""
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


def _item(item_id, manual_file):
    from navigator.observability import decisions  # decisions imports this module
    for it in decisions.read_manual(manual_file or decisions.manual_path()):
        if it["id"] == item_id:
            return it
    return None


# ---- run / show -------------------------------------------------------------------

def run_action(item_id, update_id=0, manual_file=None, actions_dir=None, log_path=None,
               now=None) -> str:
    """Run the pinned script of manual item `item_id` and return the Telegram reply.
    Refused WITHOUT executing (logged as 'refused' with the reason) when the item or
    its script is missing, check_script objects, the sha256 differs from the pinned one,
    the item ran less than MIN_INTERVAL_S ago or another action holds the lock.
    Otherwise the reply is 'ran <id>: rc=<rc>' plus the last MAX_OUTPUT_CHARS of the
    output, or 'ran <id>: timed out after <N>s'. The manual item is never removed."""
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
        timeout = RUN_TIMEOUT_S
        try:
            proc = subprocess.run(
                [script], shell=False, cwd=adir,
                env={"PATH": SAFE_PATH, "HOME": os.environ.get("HOME", "")},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            rec["outcome"] = "timeout"
            _append_log(lpath, rec)
            return f"ran {item_id}: timed out after {timeout}s"
        except OSError as e:
            return refused(type(e).__name__, f"not run: {type(e).__name__}")
    finally:
        os.close(lock_fd)  # releases the flock
    rec.update(outcome="ran", rc=proc.returncode)
    _append_log(lpath, rec)
    reply = f"ran {item_id}: rc={proc.returncode}"
    out = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    if out:
        reply += "\n" + out[-MAX_OUTPUT_CHARS:]
    return reply


def show_action(item_id, manual_file=None) -> str:
    """The pinned script of a manual item, for reading before 'run <id>': title, path,
    the first 12 hex of the pinned sha256, then the script text (capped at
    MAX_OUTPUT_CHARS, '...' appended when cut)."""
    item = _item(item_id, manual_file)
    if item is None:
        return f"no manual step {item_id}"
    script = item.get("script")
    if not script:
        return f"no script - do it yourself: {item['how']}"
    pinned = item.get("sha256") or ""
    try:
        with open(script, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"script unreadable: {type(e).__name__}"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "..."
    return "\n".join([item["title"], f"script: {script}", f"pinned sha256: {pinned[:12]}",
                      text])
