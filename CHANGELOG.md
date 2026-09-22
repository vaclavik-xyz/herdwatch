# Changelog

## Unreleased

### Added

- `claude_tasks` probe (on by default): label idle Claude Code panes that
  are still waiting on their own background work — background Bash
  commands, async subagents, Monitors, and Workflows — reconstructed from
  the session transcript. Long-running services such as dev servers are
  ignored (extend with `[probes.claude_tasks] ignore`), launches from a
  previous Claude process are discarded, and `max_age_s` caps forgotten
  tasks.
- `herdwatch doctor` warns when herdr's Agent sidebar layout does not render
  `$waiting_on`, which herdr's default layout never does.

### Fixed

- Waiting labels no longer flicker off between refreshes: their TTL has a
  180 s floor because a sweep with slow `gh`/roborev probes can take longer
  than 2 × `reprobe_interval_s`. Labels are still cleared explicitly when
  work ends.
- `herdwatch doctor` no longer crashes on a herdr config whose `ui` or
  `ui.sidebar` is a scalar.

### Compatibility

- Validated against Herdr 0.9.1 stable (protocol 22).

## 0.2.1 - 2026-08-31

### Added

- Build a wheel and source distribution for every published GitHub release.
- Attach both Python distributions to the matching GitHub release.
- Support direct `pipx` installation from the release wheel without requiring
  an external package-index account.
- Verify that the release tag, Python package, and Herdr plugin versions agree.

## 0.2.0 - 2026-08-31

### Changed

- Make TTL-backed `waiting_on` metadata the default for idle and done panes.
- Keep semantic lifecycle holds as an explicit opt-in compatibility mode.
- Make Claude Code task-file progress opt-in.
- Require Herdr 0.7.4 or newer in the plugin manifest.

### Added

- Verify the consumed Herdr socket API against SHA-256-verified stable and
  preview binaries in CI.
- Release legacy semantic assertions when a metadata-only daemon adopts state
  written by an older version.

### Compatibility

- Validated against Herdr 0.8.2 stable and preview build
  `2026-08-19-b5c4a0176e91` (protocol 20).
- Herdeck continues to derive `WAITING` from the `waiting_on` token without
  requiring herdwatch to own the pane lifecycle.

## 0.1.0 - 2026-07-12

- Initial public release.
