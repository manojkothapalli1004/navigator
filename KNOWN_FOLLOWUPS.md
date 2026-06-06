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

## REMAINING (genuine v1 / intentional)

- **Config**: safety-critical fields (`max_cost_usd`, `max_tool_failures`, …, `allowed_commands`)
  are still not env-wired — deliberate for v0's fixed safe defaults. Wire env overrides if v1
  needs per-deploy tuning.
- **Lock**: no stale-lock TTL / PID-liveness recovery if the holding process dies. Acceptable
  for v0 single-process use; add a recovery path in v1.
- **Command runner**: git subcommand flags like `git log --all/--format` remain allowed (they
  stay within the repo and don't read arbitrary files, so lower-risk than the now-closed
  `--no-index`). Consider exact-argv allowlisting for git as a v1 tightening.
- **Outcome store**: `_write_outcome` returns an `Outcome` that's discarded — there is no Outcome
  persistence in v0 (lessons go to ORC memory only). Add a store in v1 if needed.
