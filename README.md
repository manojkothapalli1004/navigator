# Navigator

Navigator is a **stateless executor** for the ORC/ACT stack. ORC is the brain (canonical
memory, decisions); ACT is durable truth + the operator console. Navigator **perceives**
(ORC memory + ACT agent resource + git state), **proposes** work into ACT, **executes**
operator-approved proposals inside per-run git worktrees with native-commit checkpoints,
**gates merges** behind operator approval, and writes **outcomes** back to ORC.

No YOLO, no auto-merge, no general shell. Every change reaches your branch only through two
explicit operator decisions: approve the proposal, then approve the merge.

## Lifecycle

```
planner proposes ──► proposal (proposed) ──approve──► run: build → verify ──► awaiting_review
                         │ expires after 7 days                │ fails → worktree preserved
                         └─ decline                            ├─ merge  → merged into target branch
                                                               └─ reject → aborted, worktree kept
```

Each daemon cycle (default every 15 min): expire stale proposals → plan (skipped while
`NAVIGATOR_MAX_OPEN_PROPOSALS` are open) → execute approved proposals → act on merge decisions.

## Operator commands

```bash
cd ~/calwbot/navigator
.venv/bin/python -m navigator status            # open proposals, approved work, open runs
.venv/bin/python -m navigator approve <proposal_id>
.venv/bin/python -m navigator decline <proposal_id>
.venv/bin/python -m navigator diff    <run_id>  # review a run parked at the merge gate
.venv/bin/python -m navigator merge   <run_id>  # applied on the daemon's next cycle
.venv/bin/python -m navigator reject  <run_id>
.venv/bin/python -m navigator retry   <proposal_id>  # one more run after a failure
```

Commands talk to ACT (`NAVIGATOR_ACT_BASE_URL`, default `http://127.0.0.1:8090`). The daemon
acts on recorded decisions; with Telegram configured it messages you when a run is ready for
review, fails, is merged, or when cycles keep failing.

## Safety model

- **Gates** (`safety/approval_engine.py`): execution requires an approved proposal (review
  gate); merging requires an approved merge approval. Architecture/destructive/budget/deploy
  gates are schema-only and raise `GateNotImplemented`.
- **Scope**: the builder may only write the files the approved proposal names
  (`affected_files`); writes are confined to the run's worktree.
- **Command runner** (`safety/command_runner.py`): argv-only allowlist (no shell, no
  metacharacters, no absolute paths, exact safe-flag set for git). A rejected command is a
  verification failure. This is allowlist confinement, **not** an OS sandbox.
- **Circuit breakers**: run duration, failed-test count, tool failures. **Budget guard**: checked
  before every LLM call, per run.
- **Merge preflight**: the main checkout must be on the run's target branch with no tracked
  edits, otherwise the run stays parked (`run.merge_blocked`). A conflicting merge is aborted,
  leaving the checkout clean, and fails the run.
- **Restart safety**: runs parked at the merge gate survive daemon restarts; runs interrupted
  mid-build are aborted with their worktree preserved.
- Subagent sessions run with `allowed_tools=[]` and `setting_sources=[]`: the model only sees
  what Navigator hands it (proposal, affected file contents, allowlist) and only returns JSON.

## Running

```bash
uv sync --extra sdk                 # claude-agent-sdk >= 0.2.165 (uv.lock pins it)
.venv/bin/python -m navigator --once        # one cycle, JSON summary on stdout
.venv/bin/python -m navigator               # daemon: one JSON line per cycle on stdout
```

Deployed as launchd `com.calwbot.navigator` (template: `deploy/launchd/`). Restart with
`launchctl kickstart -k gui/$(id -u)/com.calwbot.navigator`. Structured logs go to stderr.

## Configuration

All settings are `NAVIGATOR_*` environment variables read by `Settings.from_env()`; see
`navigator.toml.example` for every variable and its default. The code default is the safe
profile `fake` driver + `dry_run`. The live deployment uses `claude_sdk` with
`NAVIGATOR_SDK_MODEL=claude-opus-5-5`.

## Develop / test

```bash
uv run pytest -q          # deterministic: no network, no API key, fake ACT + fake LLM
```

## Layout

```
navigator/        config, cli, contracts/, clients/, safety/, runtime/, observability/
tests/            mirrors the package; tests/test_e2e.py drives the full operator journey
act-patch/        ACT agent-resource patch (applied to ACT 2026-07-02) + apply README
deploy/launchd/   daemon plist template
KNOWN_FOLLOWUPS.md       resolved + remaining work
```

## Not built yet (v1+)

Curator · ACT SSE/event push (polling is used) · ACT-backed lease (local single-flight lock is
used) · gates beyond review+merge · memory-trust tagging (needs ORC HTTP support) · general
shell (needs a real OS sandbox first).
