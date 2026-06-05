# Known follow-ups (hardening for the v1 real-driver path)

These are deliberate deferrals. None are exploitable in **v0**, because v0's only source
of commands is the deterministic `FakeSubagentDriver` (it emits a single fixed
`python -m py_compile navigator_touch.py`). There is no LLM-generated / adversarial argv
in v0. Harden these before enabling the real Claude Agent SDK driver in v1.

## Command runner (`navigator/safety/command_runner.py`) — primary safety control
- **`git diff --no-index` / explicit-path subcommands bypass the cwd restriction.**
  Prefix-matching `"git diff "` admits `["git","diff","--no-index","/etc/passwd","/dev/null"]`,
  which ignores the worktree and reads arbitrary absolute paths into stdout. Same class:
  `git log --all/--format`, `git status` path operands.
  Fix before v1: reject `--no-index` and absolute-path tokens, or switch git entries to
  exact-argv allowlisting (no prefix match).
- **No subprocess timeout; `FileNotFoundError`/`PermissionError`/`OSError` not wrapped.**
  A hung command blocks indefinitely (the `max_run_duration_s` breaker can't preempt a
  blocked `subprocess.run`). Missing binaries raise raw exceptions instead of `CommandRejected`.
  Fix before v1: add `timeout=`, catch `subprocess.TimeoutExpired`, wrap OS errors as `CommandRejected`.

## Config (`navigator/config.py`)
- `from_env()` raises a bare `ValueError` on a non-numeric `NAVIGATOR_TICK_INTERVAL_S`.
  Give a descriptive operator-facing error.
- Safety-critical fields (`max_cost_usd`, `max_tool_failures`, `max_identical_failures`,
  `max_run_duration_s`, `max_failed_tests`, `allowed_commands`) are NOT read from env by
  `from_env()` — they always use dataclass defaults. Intentional for v0's fixed safe
  defaults; document or wire env overrides if v1 needs per-deploy tuning.

## Phases (Phase 4 — robustness, not v0 bugs)
Verified non-breaking under v0's fake-driver-only path; harden before a real driver.
- **`run_build_and_verify` worktree-escape guard treats `relpath="."`/`""` as allowed**
  (resolves to the worktree root, passes the `target == wt.resolve()` branch), then
  `write_text` raises `IsADirectoryError` instead of the clean `ValueError`. Not an escape;
  the fake driver only writes `navigator_touch.py`. If the guard means "must be a file inside
  the worktree," make `target == wt.resolve()` a reject.
- **Verifier with zero commands is a silent pass**: empty `verify.commands` leaves
  `verify_returncode` at its default `0`. The fake verifier always emits one command, so v0
  never hits it. For a real driver, require ≥1 verify command or default the code to non-zero.

## Clients & runtime primitives (Phase 3 — robustness, not v0 bugs)
Verified non-breaking under v0's plan-as-written usage; harden when the surfaces get real callers.
- **`FakeACTClient.list_*` return live object references** (no `model_copy`). A future caller
  that mutates a returned object without `update_run` would silently corrupt the in-memory
  store. v0's executor/loop hold their own `run` ref + call `update_run`, and
  `set_proposal_status` mutates by id, so no current path is corrupted. Consider returning
  `model_copy()` when the fake gains more callers / richer tests.
- **`runtime/checkpoint.py: diff_artifact` lacks `check=True`** — a git failure (bad sha /
  wrong cwd) would return `""` silently. In v0 the sha is always valid and cwd is the
  worktree, so it never triggers. Add `check=True` (+ `text=True` on the add/commit calls)
  when hardening.
- **`HTTPORCClient` GET methods lack `raise_for_status()`** (POST has it). Not used in v0's
  loop (Fake is default). Add for consistency before HTTPORCClient gets real traffic.
- **`runtime/lock.py`**: fd leak if `os.write` raises (lock still released via unlink);
  no stale-lock TTL/PID recovery if the process dies. Acceptable for v0 single-process use.
- **Worktree leak on orchestration throw**: if `create_worktree` succeeds but the orchestrator
  throws before `remove_worktree`/`preserve_failed`, the dir + `nav/<run_id>` branch persist.
  v1 orchestration concern.
