"""Builds (stage 2): the conductor files a spec with Navigator through the ACT client and watches it to the end.
It never merges and never declines; the only approval it gives is the start of a tier-1 (docs/tests-only) build."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from navigator.checker.filing import ACT_REPO
from navigator.conductor.activity import _SECRET
from navigator.contracts.enums import ProposalStatus, RunPhase, RunStatus
from navigator.contracts.proposal import Proposal, compute_origin_hash
from navigator.runtime.loop import DRAFT_PREFIX, OPERATOR_ORIGIN
from navigator.runtime.merge_tiers import classify_for_repo, repo_config_for

log = logging.getLogger(__name__)

MIN_BUILD_CHARS = 400
BUILD_POLL_S = 60
BUILD_STALE_S = 24 * 3600
KEEP_FINISHED = 20
SUMMARY_MAX = 1024
OPEN_STATES = ("filed", "running", "review")
CLOSING = (ProposalStatus.declined, ProposalStatus.expired, ProposalStatus.superseded)
FAILED_RUN = (RunStatus.failed, RunStatus.rolled_back, RunStatus.aborted)

_ROOT = "/Users/Manoj/calwbot"
BUILD_REPOS: dict[str, str] = {
    name: path for name, path in {
        "navigator": f"{_ROOT}/navigator",
        "act": ACT_REPO,
        "waker": f"{_ROOT}/waker",
        "orchestrator": f"{_ROOT}/orchestrator",
    }.items() if os.path.isdir(path)
}


SPEC_MAX_BYTES = 100_000
SPEC_SUFFIXES = (".md", ".txt")
SPEC_DENY_PARTS = ("tra" + "der", "lo" + "gs")  # compared case-folded against every path component
RATIONALE = "Filed by the conductor on the operator's request."


class BuildRefused(ValueError):
    """The request cannot become a build; the message says why (shown to the model)."""


def mask(text: str) -> str:
    return _SECRET.sub("…", str(text))


def spec_roots() -> list[str]:
    """Where a spec file may live: the scratch folder, the workspace docs, each build repo's docs."""
    return [f"{_ROOT}/tmp_claude", f"{_ROOT}/docs"] + [f"{p}/docs" for p in BUILD_REPOS.values()]


def _spec_open(path: str) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError:
        raise BuildRefused("spec file not readable") from None


def read_spec(path) -> tuple[str, int]:
    """(masked text, number of masked tokens) of an allowlisted spec file; BuildRefused otherwise.
    Blocking I/O: call it from a thread, as Builds.file already is."""
    if not isinstance(path, str) or not path or "\0" in path or not os.path.isabs(path) \
            or os.path.normpath(path) != path:
        raise BuildRefused("spec_path must be a clean absolute path")
    root = next((r for r in spec_roots() if path.startswith(r + "/")), None)
    if root is None:
        raise BuildRefused("spec_path is outside the allowed folders")
    parts = path.split("/")[1:]
    if any(p.startswith(".") for p in path[len(root) + 1:].split("/")) \
            or any(p.casefold() in SPEC_DENY_PARTS for p in parts) \
            or not path.lower().endswith(SPEC_SUFFIXES):
        raise BuildRefused("spec_path is not an allowed spec file (.md or .txt, no hidden or private folders)")
    if os.path.realpath(path) != path:
        raise BuildRefused("spec_path must not go through a symlink")
    fd = _spec_open(path)
    try:
        if hasattr(fcntl, "F_GETPATH"):
            try:
                got = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode()
            except OSError:
                raise BuildRefused("spec file not readable") from None
            if got != path:
                raise BuildRefused("spec_path changed while opening")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BuildRefused("spec_path is not a regular file")
        if st.st_nlink != 1:
            raise BuildRefused("spec file must not be hard-linked")
        if st.st_uid != os.getuid():
            raise BuildRefused("spec file is not owned by the conductor's user")
        if st.st_size > SPEC_MAX_BYTES:
            raise BuildRefused(f"spec file is over {SPEC_MAX_BYTES} bytes")
        try:
            data = os.read(fd, SPEC_MAX_BYTES + 1)
            while len(data) <= SPEC_MAX_BYTES:
                more = os.read(fd, SPEC_MAX_BYTES + 1 - len(data))
                if not more:
                    break
                data += more
        except OSError:
            raise BuildRefused("spec file not readable") from None
    finally:
        os.close(fd)
    if len(data) > SPEC_MAX_BYTES:
        raise BuildRefused(f"spec file is over {SPEC_MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BuildRefused("spec file is not valid UTF-8") from None
    if "\0" in text:
        raise BuildRefused("spec file has a NUL byte")
    if len(text.strip()) < MIN_BUILD_CHARS:
        raise BuildRefused(f"a build spec must be complete (at least {MIN_BUILD_CHARS} characters)")
    masked, n = _SECRET.subn("…", text)
    return masked, n


def _head_sha(repo: str) -> str:
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True, timeout=15).stdout.strip()


def _clean_files(files) -> list[str]:
    out: list[str] = []
    for f in files or []:
        f = str(f).strip()
        if not f:
            continue
        if f.startswith("/") or ".." in PurePosixPath(f).parts:
            raise BuildRefused(f"file path must be repo-relative without '..': {f}")
        out.append(f)
    return out


def _strip_draft(title: str) -> str:
    t = title.strip()
    while t.lower().startswith(DRAFT_PREFIX):
        t = t[len(DRAFT_PREFIX):].strip()
    return t


def file_build(act, project: str, title: str, task: str, files=(), base_sha_fn=_head_sha,
               rationale: str = RATIONALE) -> Proposal:
    """File one proposal `proposed`. Always [draft]-prefixed so the daemon's pre-approve skips it; an explicit approve is unaffected."""
    repo = BUILD_REPOS.get(project)
    if repo is None:
        raise BuildRefused(f"project must be one of {sorted(BUILD_REPOS)} (trader is not available)")
    clean = _strip_draft(title or "")
    if not clean:
        raise BuildRefused("title is empty")
    if len(task or "") < MIN_BUILD_CHARS:
        raise BuildRefused(f"a build spec must be complete (at least {MIN_BUILD_CHARS} characters)")
    fl = _clean_files(files)
    filed_title = f"{DRAFT_PREFIX} {clean}"
    p = Proposal(
        origin=OPERATOR_ORIGIN,
        proposal_origin_hash=compute_origin_hash(repo, clean, [], OPERATOR_ORIGIN, task),
        title=filed_title, rationale=rationale, repo=repo,
        base_commit_sha=base_sha_fn(repo), intended_changes=task, affected_files=fl)
    return act.create_proposal(p)


class Builds:
    def __init__(self, path, act, settings=None, on_finish=None, base_sha_fn=_head_sha, loop=None,
                 on_needs_approval=None):
        self.path = Path(path)
        self.act = act
        self.settings = settings
        self.on_finish = on_finish
        self.on_needs_approval = on_needs_approval  # called (on the loop) with a record that waits for the operator
        self.base_sha_fn = base_sha_fn
        self.loop = loop
        self.current_via: str | None = None  # set by the engine at each turn start
        self._lock = threading.RLock()
        self._records: list[dict] = self._load()

    # ---- persistence
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
            recs = data.get("builds") if isinstance(data, dict) else None
            return [r for r in recs if isinstance(r, dict) and r.get("proposal_id")] if isinstance(recs, list) else []
        except (OSError, ValueError):
            return []

    def _is_open(self, r: dict) -> bool:
        return r.get("state") in OPEN_STATES or (r.get("state") == "failed" and not r.get("final"))

    def _save(self) -> None:
        open_ = [r for r in self._records if self._is_open(r)]
        done = [r for r in self._records if not self._is_open(r)][-KEEP_FINISHED:]
        keep = {id(r) for r in open_ + done}
        self._records = [r for r in self._records if id(r) in keep]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"builds": self._records}))
        os.replace(tmp, self.path)

    # ---- filing
    def _tier1(self, project: str, files: list[str]) -> bool:
        if not files or self.settings is None:
            return False
        repo = BUILD_REPOS[project]
        cfg = repo_config_for(self.settings, repo)
        if cfg is None:
            return False
        return classify_for_repo(files, repo, cfg, origin=OPERATOR_ORIGIN) == "A"

    def _approval_reason(self, project: str, files: list[str], was_draft: bool) -> str:
        """Why this build waits for the operator. Text only: tier 1 is decided by _tier1 alone."""
        if was_draft:
            return mask("the title started with [draft]; resend it without that so docs/tests-only builds "
                        "can start by themselves")
        if not files:
            return mask("no files declared; list the repo-relative paths so docs/tests-only work can start "
                        "by itself")
        repo = BUILD_REPOS.get(project)
        cfg = repo_config_for(self.settings, repo) if self.settings is not None and repo else None
        if cfg is None:
            return mask(f"no tier rules loaded for {project}, so it cannot be checked as docs/tests-only")
        bad = [f for f in files if classify_for_repo([f], repo, cfg, origin=OPERATOR_ORIGIN) != "A"]
        if not bad:
            return "the file set as a whole is not docs/tests-only"
        more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
        return mask("not docs/tests-only: " + ", ".join(bad[:10]) + more)

    def _notify_needs(self, rec: dict) -> None:
        """Ask the card store for an Approve/Decline card, on the event loop. Never raises."""
        cb = self.on_needs_approval
        if cb is None:
            return
        try:
            snap = dict(rec)
            snap["title"] = mask(rec["title"])
            snap["files"] = [mask(f) for f in rec["files"]]
            if self.loop is not None:
                self.loop.call_soon_threadsafe(cb, snap)
            else:
                cb(snap)
        except Exception as e:
            log.warning("conductor.build_card_failed: %s", type(e).__name__)

    def decide(self, proposal_id: str, approve: bool) -> None:
        """The operator's Approve/Decline on a build card (blocking). Starts nothing and merges nothing."""
        p = self.act.get_proposal(proposal_id)
        if p is None:
            raise BuildRefused("proposal not found")
        want = ProposalStatus.approved if approve else ProposalStatus.declined
        if p.status == want:
            return
        if p.status != ProposalStatus.proposed:
            raise BuildRefused(f"proposal already {p.status.value}")
        self.act.set_proposal_status(proposal_id, want)

    def file(self, project: str, title: str, task: str, files, via, spec_path=None) -> dict:
        rationale, spec_chars = RATIONALE, None
        if spec_path:  # read BEFORE any ACT call: a refused path files nothing
            if project not in BUILD_REPOS:
                raise BuildRefused(f"project must be one of {sorted(BUILD_REPOS)} (trader is not available)")
            summary = (task or "").strip()
            if not 20 <= len(summary) <= 2000:
                raise BuildRefused("with spec_path, task must be a 20-2000 character summary")
            task, _n = read_spec(spec_path)
            rationale = f"{RATIONALE} Summary: {mask(summary)[:500]}"
            spec_chars = len(task)
        fl = _clean_files(files)
        was_draft = (title or "").strip().lower().startswith(DRAFT_PREFIX)
        tier1 = (not was_draft) and project in BUILD_REPOS and self._tier1(project, fl)
        reason = None if tier1 else (self._approval_reason(project, fl, was_draft) if project in BUILD_REPOS
                                     else None)
        p = file_build(self.act, project, title, task, files=fl, base_sha_fn=self.base_sha_fn,
                       rationale=rationale)
        rec = {"spec_chars": spec_chars,"proposal_id": p.id, "project": project, "title": _strip_draft(title), "files": fl,
               "tier1": False, "state": "filed", "reply_via": via, "filed": time.time(), "ended": None,
               "summary": "", "reported": [], "approval_reason": reason}
        with self._lock:  # persisted BEFORE approving: a crash after approval must not leave it unwatched
            self._records.append(rec)
            self._save()
        if tier1:
            try:
                self.act.set_proposal_status(p.id, ProposalStatus.approved)
                with self._lock:
                    rec["tier1"] = True
                    self._save()
            except Exception as e:
                log.warning("conductor.build_autoapprove_failed: %s", type(e).__name__)
                with self._lock:
                    rec["approval_reason"] = f"auto-approve failed ({type(e).__name__}); waiting for you"
                    self._save()
        if not rec["tier1"]:
            self._notify_needs(rec)
        with self._lock:
            return dict(rec)

    def open(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records if self._is_open(r)]

    def running_work(self) -> list:
        """Builds actively running as WorkItems; filed (awaiting approval) and review (awaiting merge) builds
        live on their own branch and do not block new items."""
        from .autopilot import WorkItem
        with self._lock:
            return [WorkItem("build", r["proposal_id"], frozenset(r["files"]) if r.get("files") else None)
                    for r in self._records if r.get("state") == "running"]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    # ---- watching
    def _emit(self, rec: dict, key: str, text: str) -> None:
        """Queue the message, THEN record the key (a crash between gives a duplicate, never silence)."""
        out = dict(rec)
        out["key"] = key
        out["summary"] = mask(f"{text} [{key}]")[:SUMMARY_MAX]
        if self.on_finish is not None:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(self.on_finish, out)
            else:
                self.on_finish(out)
        rec["reported"].append(key)
        self._save()

    def poll(self) -> None:
        with self._lock:
            targets = [dict(r) for r in self._records if self._is_open(r)]
        for t in targets:
            pid = t["proposal_id"]
            try:
                p = self.act.get_proposal(pid)
                runs = self.act.list_runs_for_proposal(pid)
            except Exception as e:
                log.warning("conductor.build_poll_failed: %s", type(e).__name__)
                continue
            if p is None:
                continue
            run = max(runs, key=lambda r: r.started_at) if runs else None
            with self._lock:
                rec = next((r for r in self._records if r["proposal_id"] == pid), None)
                if rec is not None:
                    self._apply(rec, p, run)

    def _apply(self, rec: dict, p: Proposal, run) -> None:
        pid, seen = rec["proposal_id"], rec["reported"]
        title = rec["title"]

        def once(key, text):
            if key not in seen:
                self._emit(rec, key, text)

        if p.status in CLOSING and not (run is not None and run.status == RunStatus.completed):
            rec["state"], rec["ended"] = "closed", time.time()
            rec["summary"] = f"proposal {p.status.value}"
            once(f"{pid}:closed", f"build {pid} closed ({p.status.value}): {title}")
            return
        if run is None:
            rec["state"] = "filed"
            if p.status == ProposalStatus.proposed and not rec.get("tier1"):
                self._notify_needs(rec)  # repair: a restart or a failed card request left it uncarded
            if time.time() - rec["filed"] > BUILD_STALE_S:
                if p.status == ProposalStatus.approved:
                    once(f"{pid}:stale", f"build {pid} is approved but has no run yet (Navigator may be in "
                         f"dry-run, which does not build until mode is execute): {title}")
                else:
                    once(f"{pid}:stale", f"build {pid} is still waiting for approval: {title}")
            return
        if run.status == RunStatus.completed:
            rec["state"], rec["ended"] = "done", time.time()
            rec["summary"] = f"run completed (proposal {p.status.value})"
            once(f"{pid}:{run.id}:done", f"build {pid} done: {title}. {rec['summary']}")
        elif run.status in FAILED_RUN:
            rec["state"] = "failed"
            rec["final"] = p.status != ProposalStatus.approved
            rec["ended"] = time.time()
            rec["summary"] = f"run {run.status.value} in phase {run.phase.value} (proposal {p.status.value})"
            once(f"{pid}:{run.id}:failed", f"build {pid} failed: {title}. {rec['summary']}")
        elif run.phase == RunPhase.awaiting_review:
            rec["state"], rec["final"] = "review", False
            rec["summary"] = "ready for your merge OK"
            once(f"{pid}:review", f"build {pid} ({title}) is ready for your merge OK")
        else:
            rec["state"], rec["final"] = "running", False
            rec["summary"] = f"run {run.phase.value}"
        self._save()
