# Safety reports

Veridian is a single-operator preview with no outside users. Finding ways past its safety layers
is exactly the feedback this preview is asking for, so please report them in the open with the
[Safety hole](../../issues/new?template=safety-hole.yml) issue form.

The layers worth attacking:

- the conductor's approval gate (`navigator/conductor/gate.py`): which shell commands must stop for a human
- the command allowlist (`navigator/safety/command_runner.py`): what the verifier may run, argv only
- the scope check: which files an approved run may write
- the merge rules and risk tiers (`navigator/runtime/merge_tiers.py`)

The gate reads command text, so it is a strong speed bump, not a sandbox. Please don't include
working exploits against other software or people.
