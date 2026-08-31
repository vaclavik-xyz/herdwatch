# Metadata-first release direction

Status: accepted for herdwatch 0.2.0. This decision supersedes the default
semantic-hold behavior in the July 2026 design documents; those files remain as
historical implementation records.

## Decision

Herdwatch publishes pending background work through the TTL-backed
`waiting_on` metadata token by default. It does not claim lifecycle authority.
Herdr and official agent integrations remain responsible for `working`,
`blocked`, `idle`, and `done`; Herdeck derives its `WAITING` state from the
token.

Semantic `pane.report_agent` holds remain available only when
`[lifecycle] semantic_holds = true`. This compatibility mode exists for clients
that cannot interpret `waiting_on`. It verifies that Herdr applied the
assertion and falls back to metadata when an official session owner wins
authority arbitration.

Claude Code task-file progress and process-tree background-job detection are
also opt-in. The default product surface is the cross-agent core: manual
markers, Roborev, CI, and the Herdr/Herdeck waiting metadata contract.

## Compatibility gate

CI checks both `https://herdr.dev/latest.json` and
`https://herdr.dev/preview.json`. It verifies the selected binary digest,
exports `herdr api schema --json`, and fails when a consumed method, request
field, event subscription, or response field is removed or gains an
unhandled requirement. Protocol number changes alone are reported, not pinned;
the structural contract determines compatibility.

## Migration

Metadata expires if the daemon stops and therefore needs no persisted recovery.
If a 0.2.0 daemon adopts a durable semantic hold from an older state file, it
releases that assertion before publishing metadata. Users who intentionally
need the old lifecycle behavior can enable it explicitly.
