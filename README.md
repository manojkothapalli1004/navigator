# Navigator v0

Navigator is a **stateless executor** for the ORC/ACT stack. ORC is the brain (canonical
memory, decisions); ACT is durable truth + the operator console. Navigator fills the executor
slot the stack designed but never built: it **perceives** (ORC memory + ACT agent resource +
git state), **proposes** work into ACT, **executes** approved proposals inside per-run git
worktrees with native-commit checkpoints, **gates merges** behind ACT approval, and writes
**outcomes** back to ORC.

## What v0 is (and is not)

v0 is the deterministic safety/runtime core, proven end-to-end **against fakes** — there are no
live-integration claims.

- ✅ Runs end-to-end against an in-memory **`FakeACTClient`** and a deterministic
  **`FakeSubagentDriver`** (the "fake LLM"). No live LLM, no network, no API key in the test path.
- ✅ Strict **argv-only allowlist command runner** (no general shell, no shell metacharacters).
- ✅ Per-run git **worktrees** on ephemeral `nav/run-<id>` branches, with native-commit
  **checkpoints** (`navigator-checkpoint:<run_id>:<n>`), restore, and diff artifacts.
- ✅ **review + merge** approval gates only. Merges never happen without an approved merge-gate
  approval. Architecture/destructive/budget/deploy gates are declared but `NotImplemented`.
- ✅ Circuit breakers + pre-call budget guard + single-flight lock + startup reconcile.
- ✅ ORC over **REST only** (get memory / digest, apply-update). No MCP client.
- ✅ Default profile: **`NAVIGATOR_DRIVER=fake`, `NAVIGATOR_MODE=dry_run`** (dry_run proposes
  but never mutates git).

- ✅ **`HTTPACTClient` is live** (2026-07-03) — the operator applied
  `act-patch/0001-add-agent-resource.patch` to ACT on 2026-07-02 (plus follow-up support
  routes: proposal-by-id, `runs?proposal_id=&open_only=`, `POST /agent/events`, optional
  client-supplied ids). Navigator-only fields round-trip through ACT's JSON `payload`
  column. Verified end-to-end (execute → approve → merge) against a real ACT instance.

### Landed since v0 (2026-07-08)

- ✅ `ClaudeSDKDriver` (`runtime/drivers/claude_sdk_driver.py`): injectable `query_fn`, budget
  checked before every call, strict-JSON parsing (bare object only), role permissions enforced
  on outputs.
- ✅ Live claude-agent-sdk wiring (`runtime/drivers/sdk_query.py`): `NAVIGATOR_DRIVER=claude_sdk`
  + `uv sync --extra sdk`; model via `NAVIGATOR_SDK_MODEL`. Sessions run with `setting_sources=[]`
  (no operator hooks/plugins/skills leak in), `allowed_tools=[]`, `max_turns=2`; markdown fences
  stripped at the adapter so the driver parser stays strict. Smoke-tested live against haiku.
- ✅ Daemon entrypoint: `python -m navigator --once` (reconcile → periodic_tick → approval_poll,
  JSON summary on stdout). Interval daemon still v1.

### Not in v0 (deferred to v1+)

- ❌ No interval daemon (`--once` only).
- ❌ No curator, no ACT SSE/event push, no ACT-backed lease.
- ❌ No memory-trust tagging (v1.5+, once `tag_entry`/`verify_entry_result` are exposed over HTTP).

See `KNOWN_FOLLOWUPS.md` for reviewer-flagged hardening items deliberately deferred (all verified
non-triggering under v0's fake-driver-only path).

## Layout

```
navigator/        package: config, ids, contracts/, clients/, safety/, runtime/, observability/
tests/            deterministic, no network, no API key (mirrors the package)
act-patch/        SEPARATE unapplied ACT agent-resource patch + apply README
deploy/launchd/   v1 daemon plist template (do not load without approval)
navigator.toml.example   config reference (every NAVIGATOR_* var + safe default)
KNOWN_FOLLOWUPS.md       deferred hardening, by phase
```

## Develop / test

```bash
cd ~/calwbot/navigator
uv run pytest -q          # full deterministic suite
```

## Configuration

v0 reads configuration from `NAVIGATOR_*` environment variables via `Settings.from_env()`.
See `navigator.toml.example` for every variable and its safe default. The default profile is
`fake` + `dry_run`, which proposes work but performs no git mutation and contacts no live service.
