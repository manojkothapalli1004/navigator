# ACT agent-resource patch (UNAPPLIED artifact)

This directory contains a single patch, `0001-add-agent-resource.patch`, that adds the
**agent resource** (proposals / runs / approvals tables + REST API + Alembic migration +
tests + a `navigator` event source) to the Agent Control Tower (ACT) backend.

> **Navigator does NOT apply this patch.** It is generated from a scratch clone and committed
> here as a reviewable artifact. **The operator reviews and applies it manually.** Navigator's
> `HTTPACTClient` is inert (every method raises `NotImplementedError`) and becomes usable
> **only after this patch lands and the migration is run**. Until then, Navigator runs against
> the in-memory `FakeACTClient`.

## Pinned base

- **BASE_SHA:** `7ed27b511692801921985ed0c93b36671a99ddf9` (ACT `main`, clean working tree)
- Generated from a scratch clone on branch `agent-resource` (4 commits, 9 files, +1142 lines).
- Validated: `git apply --check` and `git am` both apply cleanly to BASE_SHA; the full ACT
  suite goes 186 → 198 passing (12 new agent tests); zero new ruff/mypy violations.

## What it adds

- **Models** (`backend/act/persistence/models.py`): `AgentProposalRow` (`agent_proposals`),
  `AgentRunRow` (`agent_runs`), `AgentApprovalRow` (`agent_approvals`). `String(64)` ids
  (Navigator ids are prefixed, e.g. `proposal-<ulid>`), explicit queryable columns + a JSON
  `payload` column, indexes via `__table_args__` — mirrors `EventRow`.
- **Repositories** (`repositories.py`): `AgentProposalRepository`, `AgentRunRepository`
  (incl. `list_open` → status==executing), `AgentApprovalRepository`.
- **API** (`api/v1/agent.py`, registered in `api/v1/__init__.py`; schemas in `schemas.py`):
  - `POST /agent/proposals`, `GET /agent/proposals` (optional `status` filter)
  - `POST /agent/proposals/{id}/status`
  - `POST /agent/runs`, `GET /agent/runs` (open runs only), `POST /agent/runs/{id}`
  - `POST /agent/approvals`, `POST /agent/approvals/{id}/decide`, `GET /agent/approvals`
    (filter by `target`/`gate`/`decision`)
  - All list endpoints clamp `limit` to `MAX_LIMIT=500`; missing ids return 404.
- **Events** (`events/navigator.py`): `NAVIGATOR_SOURCE = "navigator"`. Each create/transition
  writes an `EventRow` **into the request session** (so the event and the state change commit in
  one transaction), then best-effort publishes to the bus. Kinds: `agent.proposal.created`,
  `agent.proposal.status_changed`, `agent.run.created`, `agent.run.updated`
  (severity `error` when run status is `failed`), `agent.approval.created`,
  `agent.approval.decided`. `correlation_id` defaults to the entity id if the request omits one.
- **Migration** (`migrations/versions/0004_add_agent_resource.py`): revision `a1b2c3d4e5f6`,
  `down_revision = "f5d07542c346"` (ACT head at BASE_SHA — `0003_video_v0_tables`). Hand-written
  `op.create_table` for the 3 tables + indexes, with a real `downgrade()`. New Row classes are
  added to `migrations/env.py`. Verified: `upgrade head` then `downgrade -1` round-trips cleanly.
- **Tests** (`tests/api/v1/test_agent.py`): 12 tests — round-trip create/list, status filtering,
  open-run drop-out after completion, approval decide fields, 404s, and event emission
  (asserts `EventRow WHERE source='navigator'`, including the failed-run error-severity path).

## Review checklist (before applying)

- [ ] BASE_SHA still matches ACT head (`git -C <act> rev-parse HEAD`). **If ACT head has moved,
      re-point the migration's `down_revision` to the new head** before applying, or regenerate.
- [ ] Diff review of the 3 Row models + the migration agree on column names/types/nullability
      (they were verified to match `create_all` at generation time).
- [ ] Route surface is acceptable; note the routes are registered **unconditionally** (no feature
      flag) — Navigator's client needs them always available, matching the alerts/events routers.
- [ ] Soft references are intentional: run.create does not FK-validate `proposal_id`, and approval
      create does not validate `target` existence. Navigator is the sole trusted client.

## Apply commands (operator runs these — Navigator never does)

```bash
cd ~/external-tools/agent-control-tower
git checkout -b agent-resource
git apply --check /Users/pradeep/calwbot/navigator/act-patch/0001-add-agent-resource.patch   # dry-run validate
git am < /Users/pradeep/calwbot/navigator/act-patch/0001-add-agent-resource.patch
cd backend
uv run alembic upgrade head
uv run pytest -q tests/api/v1/test_agent.py
```

No new dependencies and no new env vars are introduced (`structlog` is already an ACT dep).

## After it lands

Point Navigator at ACT and switch the driver/client config so `HTTPACTClient(base_url=...)` is
used instead of `FakeACTClient`. The REST surface above is what `HTTPACTClient` must call; wiring
the real client methods to these routes is a v1 task (the v0 `HTTPACTClient` is an inert stub).
