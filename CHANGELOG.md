# Changelog

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
