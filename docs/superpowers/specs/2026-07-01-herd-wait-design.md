# herd-wait — design (v1)

**Date:** 2026-07-01
**Status:** approved, pre-implementation

## Problem

[herdr](https://herdr.dev) derives an agent's status purely from the visible
terminal of its pane, via declarative detection rules (`~/.local/state/herdr/agent-detection/remote/<agent>.toml`).
When a coding agent finishes its turn it returns to the idle `❯` prompt, so herdr
marks the pane `idle` (and emits a `done` pulse on the working→idle transition).

That is misleading when the agent finished but **background work it triggered is
still running** — CI after a merge/push, a post-commit `roborev` review, a
long-running background job. The pane looks "ready", you click it expecting
completion, and it is not actually done.

herdr cannot know this on its own: the terminal shows a finished prompt with no
trace of the background work.

## Goal

While background work is pending for a pane's repo/commit, keep that pane shown
as **`working` + a `custom_status` label** (e.g. `⏳ CI: ci`) instead of
`idle`/`done`. Release it back to the real state only when the background work
completes — at which point the natural `done` pulse fires, which is exactly when
the user *should* be told it is finished.

No changes to herdr's core and no changes to the agents. Agent-agnostic (works
for any agent herdr tracks, e.g. Claude and Codex).

## Verified feasibility (experiments against herdr 0.7.0)

The socket method `pane.report_agent` from a custom `source` is **authoritative
and durable** over screen detection:

- Reporting a state onto a pane that screen-detection actively classifies
  otherwise **overrides** it, and the override **persists** across subsequent
  detection cycles (held ≥9 s with no re-assert needed).
- `custom_status` (≤32 chars) rides along and is visible via `pane.get`.
- `pane.release_agent` drops the custom source; the pane shows `unknown` for
  ~2 s, then screen-detection **self-heals** to the real current state.
- Reporting `idle` right after `working` momentarily yields `done` — confirming
  `done` is herdr's "just finished" pulse derived from the working→idle edge.

This removes the only real risk (that screen detection would re-assert `idle`).
It does not.

## Non-goals (v1)

- No new *semantic* herdr state. herdr's state set is closed
  (`working/idle/blocked/unknown/done`); we reuse `working` + `custom_status`.
- No herdr core PR. (A small upstream PR may follow only if a concrete gap is
  hit; not required for this feature.)
- No herdr plugin packaging yet (see Future).
- No local model in the detection path (see Future / GB10).

## Architecture

A standalone Python package **`herdwait`** (new repo at
`/Users/admin/projects/herd-wait`), exposing:

- **`herd-wait daemon`** — long-lived process. Primary trigger: **subscribe to
  the herdr socket event `pane.agent_status_changed`** for low latency (reacts
  before herdr's premature `done` notification can fire). A periodic re-probe
  timer (~10–15 s) re-checks panes already under management and acts as a
  safety net for missed events. The socket-streaming client reuses the pattern
  from herdeck (`src/herdeck/protocol.py`, `connector.py`) as a reference.
- **`herd-wait` CLI** — `add` / `list` / `rm` manual markers, `status`,
  `daemon`.
- **launchd** user agent keeps the daemon alive.

All state reports use `source = "herdwait"` via the herdr CLI
(`herdr pane report-agent` / `release-agent`), which is also the documented
plugin API surface.

### Components

```
herdwait/
  daemon.py        # event loop: subscribe + timer, owns the managed-pane set
  herdr_client.py  # socket subscribe + request/response (agent list, pane get, report-agent, release-agent)
  panes.py         # PaneContext: pane_id, agent, cwd, repo, branch, head_sha, terminal_id
  probes/
    base.py        # Probe protocol: check(ctx) -> Pending(label, priority) | None
    roborev.py
    ci.py
    bgjobs.py
    marker.py
  aggregate.py     # combine probe results -> the asserted custom_status (≤32 chars)
  markers.py       # read/write ~/.local/state/herd-wait/markers/
  config.py        # ~/.config/herd-wait/config.toml
  cli.py           # argparse entrypoints
```

## State machine (per pane)

1. **Trigger** — pane transitions to `idle`/`done` (event), or a managed pane's
   re-probe timer fires.
2. **Probe** — build `PaneContext` (cwd from `pane.get`/`agent list`, then
   `git -C <cwd> rev-parse HEAD` / `branch --show-current`), run the enabled
   probes.
3. **Assert** — if ≥1 probe is *pending* and the pane is currently shown
   `idle`/`done`: `report-agent <pane> --source herdwait --agent <a>
   --state working --custom-status "<label>"`. Add pane to the managed set.
4. **Maintain** — while managed, re-probe every ~15 s; update `custom_status`.
   If the pane is independently driven to `working`/`blocked` by another
   source (the agent started real work), **step aside**: stop managing, do not
   release (we never owned that transition).
5. **Release** — when **all** probes clear: `release-agent <pane> --source
   herdwait`. Brief `unknown` self-heals to the real `idle`/`done`; the
   resulting `done` pulse is the desired "now it's really finished" signal.

Guard rails:
- Only assert when herdr currently shows `idle`/`done` for the pane.
- Never assert `blocked` in v1 (avoids false "needs attention" nags); waiting is
  semantically `working`.
- Idempotent: re-asserting the same (state, custom_status) is a no-op.

## Probes

Each probe implements `check(ctx) -> Pending(label, priority) | None`.
Priority orders which label wins when several are pending.

| Probe | Mechanism | Pending label | Notes |
|---|---|---|---|
| **roborev** | `roborev status --json` as a cheap gate (queue empty → skip); else `roborev list --json` → job for repo + HEAD sha in queued/running | `⏳ review` | exact json fields to confirm at impl; `roborev stream` is a possible future push source |
| **ci** | `gh run list --json status,headSha,workflowName --branch <b>` → run for HEAD sha with `status∈{queued,in_progress}` | `⏳ CI: <wf>` | skip if cwd not a git repo or no GitHub remote; needs `gh` auth |
| **bgjobs** | best-effort: enumerate the pane's pty child processes (via `terminal_id`→pty→`ps`), excluding the agent, alive > N s | `⏳ <cmd>` | least reliable (`pane process-info` returned empty in testing); conservative defaults; if too noisy → default-off and rely on markers |
| **marker** | read `~/.local/state/herd-wait/markers/`; a marker is pending while its `--until '<shell test>'` exits 0 / `--pid` alive / `--ttl` not expired | `⏳ <label>` | manual escape hatch; also usable by an agent that wants to push |

### Aggregation
- Combine all *pending* results; the highest-priority label is shown.
- If multiple distinct labels, show the top one or a compact `⏳ N×` form.
- Always truncate the final `custom_status` to 32 chars.

## Performance (designed for ~15 concurrent panes)

- Run the **full probe set only on the idle/done edge**, then on a slow re-probe
  cadence (~15 s) while managed — not every event tick.
- **Cache `ci`/`roborev` results keyed by `(repo, head_sha)`** with a short TTL
  so panes in the same repo share one `gh`/`roborev` call.
- Use `roborev status --json` as a cheap global "is the queue active at all?"
  gate before any per-repo roborev lookups.
- Skip probes that don't apply (no git repo, no GitHub remote, probe disabled).

## Configuration

`~/.config/herd-wait/config.toml`:

```toml
[daemon]
reprobe_interval_s = 15
socket_path = ""          # default: $HERDR_SOCKET_PATH or ~/.config/herdr/herdr.sock

[probes]
roborev = true
ci      = true
bgjobs  = true            # best-effort; set false if noisy
marker  = true

[probes.ci]
cache_ttl_s = 10

[probes.bgjobs]
min_age_s = 5

[panes]
allow = []                # empty = all panes
deny  = []
```

## CLI

```
herd-wait daemon                         # run the watcher (managed by launchd)
herd-wait status                         # show managed panes + active markers
herd-wait add "<label>" [--pane ID] [--until '<cmd>' | --pid N | --ttl S]
herd-wait list                           # list active markers
herd-wait rm <marker_id|--all>
```

A marker with no explicit `--pane` binds to the caller's `$HERDR_PANE_ID` (so an
agent can `herd-wait add "deploy" --until 'check.sh'` from inside its own pane).

## Packaging / runtime

- Python ≥3.11, stdlib + `tomllib`; no heavy deps. `gh` and `roborev` invoked as
  subprocesses; herdr via its CLI (`$HERDR_BIN_PATH` or `herdr` on PATH).
- launchd user agent `~/Library/LaunchAgents/dev.herdwait.daemon.plist` runs
  `herd-wait daemon` with `KeepAlive`.

## Edge cases & risks

- **Notification race** — event subscription minimizes it, but a premature
  `done` could theoretically slip in a sub-second window before we assert.
  Accepted for v1.
- **release → unknown blip** (~2 s) before self-heal — verified harmless;
  documented. We prefer `release-agent` over leaving stale authority.
- **Agent resumes real work while managed** — detect via independent
  `working`/`blocked` from another source and step aside.
- **bgjobs false positives/negatives** — the weakest probe; conservative
  defaults, can be disabled, markers cover the gap.
- **gh/roborev unavailable or unauthenticated** — probe degrades to "not
  pending" (never blocks the pane on a broken tool).
- **Pane/session disappears** — drop from managed set on `pane.exited`/closed.

## Open items to confirm during implementation

1. Exact `roborev list --json` shape (job → sha mapping, status vocabulary).
2. Reliable enumeration of a pane's background child processes for `bgjobs`.
3. Whether to additionally set `state_labels` via `pane.report_metadata` for
   nicer wording than the bare `working` label (cosmetic; default off in v1).

## Testing strategy

- **Unit**: each probe against fixtured `gh`/`roborev`/`ps` outputs (pending vs
  clear vs tool-missing). Aggregation + 32-char truncation. Marker
  expiry/`--until`/`--pid` logic.
- **State machine**: a fake herdr client (assert/release recorded) driven
  through idle→pending→clear and the step-aside path; assert idempotency and
  correct release.
- **Integration (manual, gated)**: against a real herdr session on a throwaway
  split pane, mirroring the feasibility experiments — assert, observe via
  `pane get`, release, confirm self-heal. Never targets the user's live agents.

## Future (out of v1)

- **herdr plugin wrap** — ship `herdr-plugin.toml` so others can
  `herdr plugin install <owner>/herd-wait`; the daemon stays the core, the
  plugin provides install + actions.
- **GB10 / local model (opt-in)** — only as a label/summary formatter ("turn the
  CI log tail into a short human `custom_status`/message"), never as the
  detector. Default off.
- Possible upstream PR only if a concrete herdr gap is found (e.g. a richer
  plugin event hook); not required for this feature.
