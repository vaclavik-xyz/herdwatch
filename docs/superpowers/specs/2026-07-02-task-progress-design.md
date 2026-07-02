# Task progress in the sidebar — design

Date: 2026-07-02
Status: approved

## Problem

When an agent works through a multi-step task list, the herdr sidebar only
shows `working`. The user cannot tell how far along the agent is ("task 3 of
7") without switching to the pane. herdwatch already owns the "decorate a
pane's status from outside" mechanism, so it is the natural place to add
progress display.

## Goals

- While a Claude Code agent is actively working through a task list, the
  sidebar shows `3/7 <current task name>` for that pane.
- The moment the agent actually stops (finished, or waiting for the user),
  the label disappears and the pane's real status shows — the feature must
  never hide "this pane needs your attention".
- No per-agent or per-project setup, consistent with herdwatch's design.

## Non-goals

- Progress for non-Claude agents (codex etc. have no readable task store).
- Event-driven updates (stays within the daemon's poll loop).
- Rendering changes in herdr itself.

## Mechanism findings (verified experimentally)

1. Claude Code persists task lists to
   `~/.claude/tasks/session-<uuid[:8]>/<N>.json`, one file per task, with
   `subject`, `activeForm`, and `status` (`pending` / `in_progress` /
   `completed`). herdr's `agent list` exposes `agent_session.value` — the
   full session UUID — so pane → task list resolution needs no cooperation
   from the agent.
2. `herdr pane report-metadata --custom-status` does NOT render in the
   sidebar (tested with `--applies-to-source`, `--agent`, `--state-label`
   variants). The only rendering path is `herdr pane report-agent
   --custom-status`, which asserts a state.
3. A `report-agent` assertion masks herdr's own detection for as long as the
   source holds it, but `herdr agent explain --json` returns the live
   screen-detection result (`state` field) regardless of asserted sessions.
   This gives the daemon ground truth for "is the agent really still
   working" while its own assertion is active.

## Architecture

### New module: `src/herdwatch/progress.py`

`read_progress(session_id: str) -> Progress | None`

- Resolves `~/.claude/tasks/session-<session_id[:8]>/`, parses the numeric
  `*.json` files (malformed or non-dict files are skipped).
- Returns `Progress(done, total, active)` where `done` = count of
  `completed` tasks and `active` = the `activeForm` (fallback `subject`) of
  the first `in_progress` task in numeric file order.
- Returns `None` (no label) unless **total >= 2 and at least one task is
  `in_progress`**. The `in_progress` requirement filters both finished
  lists and stale lists left over from earlier requests in the same
  session.

`format_label(p: Progress) -> str`

- `f"{min(p.done + 1, p.total)}/{p.total} {p.active}"`, truncated to the
  existing 32-character label limit (`aggregate.MAX_LEN`, moved to a shared
  constant) with an ellipsis.

### Daemon integration

`ManagedPane` gains `kind: str` — `"hold"` (existing ⏳ behaviour) or
`"progress"`. The kind is persisted in the state-file snapshot; `adopt()`
defaults missing kinds to `"hold"` (backward compatible with old
snapshots).

Per-pane tick logic:

1. Determine the pane's true status:
   - pane managed with `kind="progress"` → call `agent explain` (via a new
     `HerdrClient.agent_explain(pane_id) -> str | None` returning the
     `state` field); our own assertion masks `agent_status`, detection does
     not lie. On any failure treat as "not working" (release rather than
     mask).
   - otherwise → `agent_status` from `agent list` is already the truth.
2. True status `working`, agent is `claude`, session id known, progress
   config enabled → `read_progress`; if a label results, `report-agent
   working` + label (re-report only on label change, reusing the existing
   dedupe); if no label and we hold a progress assertion → release.
3. True status not `working` → release any progress assertion, then fall
   through to the existing idle/done probe flow **in the same tick**, so a
   ⏳ hold (CI/review) can take over seamlessly.

Existing hold behaviour, reprobe throttling, vanished-pane release,
`release_all`, and restart adoption are unchanged; progress-managed panes
ride the same bookkeeping.

The reprobe throttle (`reprobe_interval_s`, default 15 s) does not apply to
the progress path — reading a local directory and one `agent explain` per
decorated pane is cheap, and progress should update at poll cadence (~4 s).

### Session resolution

`agent list` entries carry `agent_session.value`. The daemon passes it into
`PaneContext` as a new optional field `session_id: str | None` (None when
absent). Only entries with `agent == "claude"` are considered for progress.

## Config

```toml
[probes]            # unchanged

[progress]
enabled = true      # default on; set false to disable the feature
```

Progress respects the existing `[panes] allow` / `deny` lists. No other
tuning knobs in v1 (label length — the shared 32-char limit — and the
total >= 2 threshold are constants).

## Edge cases

- **Task forgotten in `in_progress`** after the agent moved on: the label
  can wrongly persist while the pane works on something else. Cosmetic;
  accepted for v1.
- **Brief gaps between tasks** (nothing `in_progress`): label disappears
  until the next task starts. Accepted — simple and predictable.
- **`agent explain` failure or warning**: treated as "not working" →
  release. A one-tick label flicker is preferred over masking a waiting
  pane.
- **Daemon restart**: progress panes are adopted from the state file; the
  next tick re-derives truth (explain + task list) and re-asserts or
  releases, same as holds.
- **Clean shutdown**: `release_all` already releases every managed pane.
- **Sub-poll-interval staleness**: the label lags reality by up to one
  poll interval (~4 s), same as every herdwatch signal.

## Testing

- `progress.py`: tmp-dir task fixtures — counts, active-task selection,
  malformed JSON skipped, all-completed → None, single task → None,
  truncation.
- Daemon: fake client + fake explain — asserts label while detection says
  working; releases when detection flips; hands over to a CI hold in the
  same tick; re-report only on label change; restart adoption with and
  without `kind` in the snapshot; `[progress] enabled=false` disables.
- Config: parsing `[progress]`.
- Live verification: run the daemon while a Claude agent works through a
  task list; watch the sidebar label advance and vanish on completion.
