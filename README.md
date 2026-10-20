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

## Telegram reader operations

Two launchd services share one bot:

- `com.calwbot.navigator` - the daemon (`python -m navigator`). It polls Telegram
  `getUpdates` only when `NAVIGATOR_TELEGRAM_COMMANDS=1`.
- `com.calwbot.navigator-telegram` - the always-on reader (`python -m navigator
  telegram-reader`, template `deploy/launchd/com.calwbot.navigator-telegram.plist.example`).
  It long-polls `getUpdates` continuously and answers status / approve / decline / merge
  at once instead of once per daemon cycle.

Telegram allows **one `getUpdates` consumer per bot token**. While the reader runs, keep
`NAVIGATOR_TELEGRAM_COMMANDS` unset (or `0`) on the daemon; the reader plist example sets no
such key at all. Never run two consumers.

### Reader refusals (exit 2)

The reader prints one line on stderr and exits 2 when it must not start:

- `refusing to start - NAVIGATOR_TELEGRAM_COMMANDS=1 makes the daemon poll getUpdates and
  Telegram allows one consumer per bot; unset it to run the reader` - the variable is set in
  the reader's own environment; remove it from the reader plist.
- `no Telegram credentials (NAVIGATOR_TELEGRAM_BOT_TOKEN + NAVIGATOR_TELEGRAM_CHAT_ID or
  NAVIGATOR_NOTIFY_ENV_FILE)` - point `NAVIGATOR_NOTIFY_ENV_FILE` at the env file holding
  `TELEGRAM_BOT_TOKEN` / `AUTHORIZED_CHAT_ID`, or set the two variables directly.
- `refusing to start - another telegram-reader is running (lock <path>); Telegram allows one
  getUpdates consumer per bot` - a live process holds the reader lock. A lock left by a dead
  process is reclaimed automatically; a lock held by a live reader means you already have one.

### Environment

| Variable | Meaning | Default |
|---|---|---|
| `NAVIGATOR_NOTIFY_ENV_FILE` | env file with `TELEGRAM_BOT_TOKEN` and `AUTHORIZED_CHAT_ID` | none |
| `NAVIGATOR_TELEGRAM_BOT_TOKEN` + `NAVIGATOR_TELEGRAM_CHAT_ID` | credentials set directly (take precedence over the env file) | none |
| `NAVIGATOR_ACT_BASE_URL` | ACT the reader records decisions in | `http://127.0.0.1:8090` |
| `NAVIGATOR_TELEGRAM_OFFSET_PATH` | persisted `getUpdates` offset | `/Users/Manoj/calwbot/logs/navigator/telegram_offset` (`DEFAULT_OFFSET_PATH` in `navigator/observability/telegram_inbox.py`) |
| `NAVIGATOR_TELEGRAM_INBOX_PATH` | JSONL inbox of operator messages | `/Users/Manoj/calwbot/logs/navigator/telegram_inbox.jsonl` (`DEFAULT_INBOX_PATH` in `navigator/observability/telegram_inbox.py`) |
| `NAVIGATOR_TELEGRAM_READER_LOCK` | single-flight lock of the reader | `<offset path>.reader.lock`, i.e. `/Users/Manoj/calwbot/logs/navigator/telegram_offset.reader.lock` |

The offset and inbox defaults are the absolute `DEFAULT_OFFSET_PATH` and
`DEFAULT_INBOX_PATH` constants in `navigator/observability/telegram_inbox.py`; they do not
depend on the plist's `WorkingDirectory`.

**Warning:** the daemon (`make_telegram_inbox`), the reader and `navigator inbox` all fall
back to the same two constants. If you override `NAVIGATOR_TELEGRAM_OFFSET_PATH` or
`NAVIGATOR_TELEGRAM_INBOX_PATH`, set the SAME values in the daemon plist, in the reader
plist and in the shell that runs `navigator inbox` - override all three together or none.
A reader writing one inbox file while `navigator inbox` reads another silently loses
messages; two offset files mean replayed or skipped updates.

### 409 Conflict

`TelegramError: another Telegram getUpdates consumer is active` in the reader log means a
second consumer is polling the same bot token. The reader already copes: it retries every
30 s and sends you at most one Telegram notice per hour per error kind, so there is nothing
to restart. The fix is to find and stop the other consumer: the daemon with
`NAVIGATOR_TELEGRAM_COMMANDS=1`, a second reader on another machine or checkout, a
notebook or shell calling `getUpdates` by hand. Once it stops, the next retry succeeds.

### Log files

The reader logs to whatever `StandardOutPath` / `StandardErrorPath` its plist sets. The
example uses `logs/navigator/telegram-reader.out.log` and
`logs/navigator/telegram-reader.err.log` under the repo
(`/Users/YOU/calwbot/logs/navigator/`). Structured warnings
(`telegram_reader.poll_failed`, `telegram_reader.handle_failed`) go to stderr and name
exceptions by class only: the `getUpdates` URL carries the bot token.

### doctor

`navigator doctor` checks only the daemon plist (`~/Library/LaunchAgents/com.calwbot.navigator.plist`,
or the path given with `--plist`), not the reader's. Verify the reader plist by hand:
`plutil -lint` it, then read its log after `launchctl bootstrap`.

### Restart and rollback

```bash
# code-only change (no plist edit): restart in place
launchctl kickstart -k gui/$(id -u)/com.calwbot.navigator-telegram

# after ANY plist edit: launchd caches the plist, so unload and load it again
launchctl bootout gui/$(id -u)/com.calwbot.navigator-telegram
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.calwbot.navigator-telegram.plist
```

The same two forms apply to the daemon label `com.calwbot.navigator`.

Rollback to daemon-only polling, in this order:

1. `launchctl bootout gui/$(id -u)/com.calwbot.navigator-telegram` - stop the reader first.
2. Optionally set `NAVIGATOR_TELEGRAM_COMMANDS=1` in the daemon plist and `bootout` +
   `bootstrap` the daemon so it polls `/merge` and `/reject` itself once per cycle.

Never the reverse order and never both at once: two consumers mean 409 for both.

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
deploy/launchd/   daemon + Telegram reader plist templates
KNOWN_FOLLOWUPS.md       resolved + remaining work
```

## Not built yet (v1+)

Curator · ACT SSE/event push (polling is used) · ACT-backed lease (local single-flight lock is
used) · gates beyond review+merge · memory-trust tagging (needs ORC HTTP support) · general
shell (needs a real OS sandbox first).
