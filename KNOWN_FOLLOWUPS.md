# Known follow-ups

## RESOLVED (hardening pass, 2026-06-06)

Each fixed via TDD (failing test first). Suite 46 → 63 tests.

- **Command runner** (`safety/command_runner.py`): rejects `--no-index` and absolute-path
  tokens (closes the arbitrary-file-read hole); adds a `timeout_s` (default 60) and wraps
  `TimeoutExpired` + `OSError`/missing-binary as `CommandRejected`.
- **Config** (`config.py`): non-numeric `NAVIGATOR_TICK_INTERVAL_S` now raises a descriptive
  `ValueError` naming the var (via `_int_env`).
- **Checkpoint** (`runtime/checkpoint.py`): `diff_artifact` uses `check=True` (no silent empty
  string on git failure); `add`/`commit` use `text=True` for readable errors.
- **FakeACTClient** (`clients/act_client.py`): `list_proposals`/`list_open_runs`/`list_approvals`
  return `model_copy(deep=True)` — mutating a returned object no longer corrupts the store.
- **HTTPORCClient** (`clients/orc_client.py`): GET `get_canonical_memory`/`get_digest` now call
  `raise_for_status()` (consistent with the POST).
- **Phases** (`runtime/phases.py`): write-target guard extracted to `_resolve_write_target`,
  which rejects the worktree root itself (`relpath="."`/`""`) and escapes with a clean
  `ValueError`; an empty verifier command list is now a failure, not a silent pass.
- **Executor** (`runtime/executor.py`): `create_worktree` is wrapped — a creation failure marks
  the run failed + writes a `known_failures` ORC lesson (no stuck `executing`, no uncaught
  raise); failure paths set `phase=finalizing`; the merged worktree is removed on the success
  path (best-effort `_cleanup_worktree`); dead double-assignment to `run.branch` removed.
- **Reconcile** (`runtime/loop.py`): `reconcile_on_startup` writes a `known_failures` ORC lesson
  per aborted stale run (post-mortem trail).
- **Lock** (`runtime/lock.py`): fd is closed in a `finally` so it can't leak if `os.write` raises.

## RESOLVED (completion pass, 2026-10-09)

Found by the first live execute-mode run and a spec-vs-code audit. Each fixed test-first.

- **Outage masking** (`drivers/sdk_query.py`): failed SDK sessions surfaced only as
  `error result: success`; 216 cycles (2026-08-25 → 10-09) failed on "Usage credits are
  required" for Fable. The real `ResultMessage.result` text is now raised; billing and
  model-support errors are not retried. Model → `claude-opus-5-5`; SDK ≥ 0.2.165 (lock-pinned).
- **Proposal spam** (`runtime/loop.py`): planner saw only proposal ids → 142 open duplicates.
  It now sees open titles, recent commits and the tracked file list; normalized-title dedup;
  `NAVIGATOR_MAX_OPEN_PROPOSALS` backpressure; unreviewed proposals expire after
  `NAVIGATOR_PROPOSAL_TTL_DAYS`.
- **Blind builder** (`runtime/phases.py`): builder/verifier got `context={}`. They now receive
  the proposal, affected file contents, and the command allowlist. Runs branch from current
  HEAD, not the proposal's stale base.
- **Merge gate never polled** (`runtime/loop.py` `merge_poll`): approved runs are merged,
  declined runs aborted (worktree kept). Merge preflight protects the operator's checkout;
  conflicts are aborted cleanly.
- **Restart discarded parked runs**: reconcile now keeps runs at `awaiting_review`; every run
  transition renews `heartbeat_at`.
- **Safety not wired** (`safety/approval_engine.py`, phases, executor): review gate enforced at
  execution; circuit breakers (duration, failed tests, tool failures) and per-run budget active;
  builder writes limited to approved `affected_files`; rejected verifier commands are
  verification failures; `--` allowed in git argv.
- **No operator surface** (`cli.py`): `status/approve/decline/diff/merge/reject/retry`.
- **Silent failures** (`observability/notify.py`): Telegram messages on review-ready, failed,
  merged, declined, merge-blocked runs, and on daemon failure streaks + recovery.
- **Prompt bloat**: ORC memory trimmed per section (~85k → ~23k chars live); recent outcomes
  included; `snapshot_chars` reported per cycle; structured logs moved to stderr.

## REMAINING (v1+, intentional)

- **Curator** (spec §11) — not built.
- **ACT push** — polling only; SSE/webhook needs an ACT change.
- **ACT-backed lease** — single host, local lock is sufficient for now.
- **Gates beyond review+merge** — schema-only (`GateNotImplemented`).
- **Memory-trust tagging** — needs ORC to expose tagging over HTTP (v1.5+).
- **General shell** — never without a real OS sandbox.
- **Verifier environment** — verifier commands run inside the run's worktree, which has no
  virtualenv; `uv run pytest` builds one per run (slow first time). Revisit if runs time out.

## History (pre-2026-10)


- **Config**: ~~safety fields not env-wired~~ RESOLVED 2026-07-14 — `NAVIGATOR_MAX_COST_USD`
  (>0), `NAVIGATOR_MAX_TOOL_FAILURES` (>=1), and `NAVIGATOR_ALLOWED_COMMANDS` (comma-separated,
  injection-guarded) are now read by `Settings.from_env()`. Remaining limits still use defaults.
- **Lock**: ~~no stale-lock recovery~~ RESOLVED 2026-07-13 — PID-liveness recovery landed
  (dead/unparseable holder reclaimed; live holder, incl. other-user PIDs, still blocks).
- **Command runner**: ~~broad git flags allowed~~ RESOLVED 2026-07-14 — LLM-issued git commands
  are limited to an exact per-subcommand safe-flag set (`_GIT_SAFE_FLAGS`); file-writing flags
  (`--output=`) and unknown flags (`--all`) are rejected. Trusted runtime git calls bypass the runner.
- **Outcome store**: ~~Outcome discarded~~ RESOLVED 2026-07-14 — `OutcomeStore` (append-only
  JSONL at `NAVIGATOR_OUTCOME_PATH`) persists each outcome; corrupt trailing lines are skipped on
  read; a store-write failure never fails the run. `latest(n)`/`for_proposal(id)` read API added
  (planner not yet wired to consult it — that is the next v1 step).
