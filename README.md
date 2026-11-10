# Navigator

Navigator is an AI coding agent that plans a change, builds it, tests it and checks its own
work, then **stops and waits for a human OK before anything is merged**.

It is a personal project, public for a short feedback window. Please read the
[status](#status) section before judging it as a product.

## Why it is different

Most coding agents optimise for autonomy. Navigator optimises for being safe to leave running:

- **Never merges on its own.** Work reaches your branch only after two human decisions:
  approve the proposal, then approve the merge. (One opt-in exception: with
  `NAVIGATOR_MERGE_TIER_A_AUTO=1`, tests/docs-only changes with green verification may be
  auto-approved. It is off by default.)
- **Risk tiers** (`runtime/merge_tiers.py`). Every run is classified from the files it touched:
  **A** = tests, fixtures, docs; **C** = money, auth, secrets, migrations, dependency files and
  Navigator's own safety code (always needs its own explicit approval); **B** = everything else.
  Unknown means stricter, not looser.
- **Scope checks.** The builder may only write files the approved proposal names, and only
  inside a per-run git worktree.
- **Command allowlist.** Argv-only, no shell, no metacharacters, no absolute paths. A rejected
  command counts as a failed verification. This is confinement by allowlist, **not** an OS sandbox.
- **Self-checks before review.** Tests run, and new tests must actually exercise the change
  (they are run against the base commit and are expected to fail there).
  Empty diffs are failures, not merge candidates.
- **Approval cards.** In the chat service, gated actions (merge, push, rebase, edits to
  settings or service files, build starts over a cost cap) are denied until a card showing the
  exact action is approved once. The approval covers that one identical call and expires.
- **Cold audits.** Safety-relevant changes are re-reviewed by a fresh session that did not write
  them, and findings are fixed before the work lands.
- **Budget and circuit breakers.** Per-run duration, failed-test and tool-failure limits;
  a dollar budget checked before each LLM call; an API-credits fallback that only engages after
  a probe confirms the subscription is really exhausted, and is capped per day.

## Parts

1. **Daemon pipeline** (`navigator/runtime`). Each cycle: expire stale proposals, plan,
   execute approved work, act on merge decisions. A *planner* proposes work, a *builder*
   writes it in a worktree with a checkpoint commit per phase, a *verifier* runs the allowlisted
   tests and checks. Failed runs keep their worktree for inspection. Proposals are ranked so the
   operator sees about three at a time.
2. **Conductor** (`navigator/conductor`). One shared conversation reachable from a web page,
   Telegram or your own terminal (the terminal joins the same session). It holds the gate,
   approval cards, build filing (it files specs with the daemon but never merges or declines),
   optional autopilot through a plan file (off by default, capped, quiet hours) and the credits fallback.
3. **Helpers** (`conductor/helpers.py`). The conversation turn only talks and delegates;
   long work (builds, audits) runs as separate short-lived sessions on a cheaper model with a
   write fence, and each reports back as a message. A registry survives restarts so lost helpers
   are reported.
4. **Site checker** (`navigator/checker`). Drives a browser through a web console, records
   faults and files fixes as proposals into the same gated pipeline.

```
planner proposes -> proposal -> (human approves) -> build -> verify -> awaiting review
                                                                 |-> (human merges) -> merged
                                                                 `-> (human rejects) -> aborted, worktree kept
```

## Shadow-mode results

To measure quality without risk, Navigator replayed real historical bug fixes from the public
[more-itertools](https://github.com/more-itertools/more-itertools) repo. It was given the issue
text at the parent commit, never merged or pushed, and was scored against the upstream tests
that it could not see.

- 17 runs, 13 distinct replays.
- **10 of 13 correct on the first pass.**
- 2 were wrong, both traced to **ambiguous issue text** (the issue did not say which
  exception should be raised; Navigator chose differently from upstream).
- 1 honest failure (it reported failing rather than claiming success).
- **0 cases of passing broken work that its own checks could have caught.**
- After tuning (large-file handling, minimal diffs, related-test context), re-runs of 4 replays
  were 4/4 correct and the average diff fell from 53 to 22 lines (~60% smaller; real upstream
  fixes average ~5).

Caveats: small sample, one repository, a library with a fast, strong test suite, and the
tuning happened on the same replays it was then re-run on. Treat this as a smoke signal, not a benchmark.

## Status

A personal project with a single operator, built for macOS and run under launchd. Several
paths and service labels are hard-coded to that setup. **Not production-ready**: no multi-user
model, no OS-level sandbox, and APIs and file formats change without notice.

## Running the tests

```bash
uv sync --extra sdk
uv run pytest -q     # deterministic: no network, no API key, fake ACT and fake LLM
```

Running the daemon for real needs a Claude subscription or API key and an ACT instance; see
[docs/RUNBOOK.md](docs/RUNBOOK.md) (operator commands, Telegram reader, configuration) and
`navigator.toml.example`. The code default is the safe profile: fake driver + dry run.

## Feedback wanted

1. **Gates:** are "two human decisions" plus risk tiers the right amount of friction? What would
   make you trust an agent enough to turn on tier-A auto-merge, or not?
2. **Verification:** the "new tests must fail on the base commit" check is meant to catch fake
   tests. What other cheap self-checks would you add?
3. **Evaluation:** is replaying historical fixes against hidden upstream tests a fair way to
   measure this? What repos or task types would make the numbers more convincing?
4. **Safety holes:** where would you attack the allowlist, scope check or approval cards first?

Open an issue, or comment wherever you found this. The repo is public for 48 hours.

## Layout

```
navigator/   config, cli, contracts/, clients/, safety/, runtime/, conductor/, checker/, observability/
tests/       mirrors the package; tests/test_e2e.py drives the full operator journey
deploy/      launchd plist templates
docs/        RUNBOOK.md (operator runbook)
```
