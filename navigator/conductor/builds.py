"""Builds (stage 2): the conductor files a spec with Navigator through the ACT client and watches it to the end.
It never merges and never declines; the only approval it gives is the start of a tier-1 (docs/tests-only) build."""
from __future__ import annotations

import json
import logging
import os
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


class BuildRefused(ValueError):
    """The request cannot become a build; the message says why (shown to the model)."""


def mask(text: str) -> str:
    return _SECRET.sub("…", str(text))


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


def file_build(act, project: str, title: str, task: str, files=(), base_sha_fn=_head_sha) -> Proposal:
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
        title=filed_title, rationale="Filed by the conductor on the operator's request.", repo=repo,
        base_commit_sha=base_sha_fn(repo), intended_changes=task, affected_files=fl)
    return act.create_proposal(p)


class Builds:
    def __init__(self, path, act, settings=None, on_finish=None, base_sha_fn=_head_sha, loop=None):
        self.path = Path(path)
        self.act = act
        self.settings = settings
        self.on_finish = on_finish
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

    def file(self, project: str, title: str, task: str, files, via) -> dict:
        fl = _clean_files(files)
        was_draft = (title or "").strip().lower().startswith(DRAFT_PREFIX)
        tier1 = (not was_draft) and project in BUILD_REPOS and self._tier1(project, fl)
        p = file_build(self.act, project, title, task, files=fl, base_sha_fn=self.base_sha_fn)
        if tier1:
            try:
                self.act.set_proposal_status(p.id, ProposalStatus.approved)
            except Exception as e:
                log.warning("conductor.build_autoapprove_failed: %s", type(e).__name__)
                tier1 = False
        rec = {"proposal_id": p.id, "project": project, "title": _strip_draft(title), "files": fl,
               "tier1": tier1, "state": "filed", "reply_via": via, "filed": time.time(), "ended": None,
               "summary": "", "reported": []}
        with self._lock:
            self._records.append(rec)
            self._save()
            return dict(rec)

    def open(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records if self._is_open(r)]

    def running_work(self) -> list:
        """Open builds as WorkItems (a build in review frees its cap slot but keeps its files busy)."""
        from .autopilot import WorkItem
        with self._lock:
            return [WorkItem("build", r["proposal_id"], frozenset(r["files"]) if r.get("files") else None,
                             r.get("state") == "review") for r in self._records if r.get("state") in OPEN_STATES]

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
            if time.time() - rec["filed"] > BUILD_STALE_S:
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
