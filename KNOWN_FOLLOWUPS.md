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
