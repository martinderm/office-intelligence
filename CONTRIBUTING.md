# Contributing to Office Intelligence

This bundle can be cloned on its own; no private Skills superproject is required.
The bundle rules in [AGENTS.md](AGENTS.md) apply alongside any stricter rules of
the consuming host workspace.

## Git and scope

- Default mode is **Review**: verify changes and leave them unstaged. Stage,
  commit and push only in explicitly selected **Full-Auto** mode.
- Inspect status first. Fetch and fast-forward a clean checkout before work if
  its last confirmed sync is over two hours old or concurrent work has occurred.
  Never reset, discard or hide somebody else's changes; use an isolated branch
  or worktree when necessary.
- All authored changes go through short-lived branches and pull requests, not
  direct pushes to `main`. Full-Auto alone is not merge authority. Independent
  review and green checks on the exact reviewed head are required. Security,
  governance, schema and other high-risk changes need an owner merge; never
  approve your own governance changes.
- Keep third-party dependencies read-only; initialize only dependencies needed
  for the task. Do not update unrelated adapters or consumer workspaces.

## Implementation and verification

- Prefer Python 3 standard-library tools with no pip dependencies. Other
  languages need a documented platform/ecosystem justification.
- Use UTF-8, LF, forward slashes and `pathlib.Path`; no host-specific paths in
  source, configuration, generated artifacts or documentation.
- CLI tools support structured JSON envelopes via `--json`. Use atomic writes
  (`tempfile` plus `os.replace`) and preserve existing data on parse/schema errors.
- Synchronize affected contracts and L1/L2 system maps in the same change.
- Keep all seven desks and their relative resource paths together. An installed
  bundle is a runtime library, never a consumer's output/state directory.
- Run desk suites with `python -B -m unittest discover -s skills/<desk>/tests`.
  Real-guard tests need the trusted `workspace-lock` installation documented in
  [README.md](README.md#workspace-lock-voraussetzung). Some historical tests also
  require it as a sibling; use that documented layout for the complete suite.
  Declare the real installation as `OI_TEST_WORKSPACE_LOCK_ROOT` for
  `python -B -m unittest discover -s tests`; these integration tests must not
  silently skip missing dependencies.
- Report actual checks, failures, skips and limits in the PR/issue; do not
  append work status to historical root feature ledgers.
