# herdwatch: migrate to the herdr socket API (event-driven daemon)

**Date:** 2026-07-11
**Status:** approved

## Goal

Replace the CLI-subprocess polling transport (`herdr agent list` every
`poll_interval_s`) with herdr's raw socket API: `session.snapshot` bootstrap +
`events.subscribe` event stream, all requests over the unix socket, zero
`herdr` subprocess spawns. At the same time move display-only labels to
`pane.report_metadata`, which removes two v1 limitations:

- **#1 Poll-based:** an idle edge now reacts in ~100 ms instead of up to 4 s.
- **#2 No ⏳ on `done` panes:** a `done` pane with pending work gets a
  display-only ⏳ label without touching its semantic state.

And softens **#5 (no "step aside")** for progress labels: they no longer mask
the agent's real lifecycle state.

## Non-goals

- No fallback to CLI polling. herdr server **≥ 0.7.2** (protocol 16, has
  `session.snapshot`) is required; older servers get a clear log + retry loop
  and a `doctor` finding.
- No Windows support (named pipes). Unix domain sockets only, matching the
  daemon's macOS/Linux scope.
- No change to probe logic (ci/roborev/marker/bgjobs), aggregation, markers,
  progress-label derivation, launchd service, or the `status`/state-file
  mechanism.
- `⏳`-holding of **idle** panes keeps using semantic `pane.report_agent` —
  that state change is the core feature, not presentation.

## Verified protocol facts (herdr 0.7.3 source, protocol 16)

These shaped the design; implementers should not need to re-derive them:

- Transport: newline-delimited JSON over a unix socket. **One request per
  connection.** A `events.subscribe` request turns the connection into a
  stream (ack response, then pushed event lines); every other method is
  request → response → close. Server reads the initial request with a 5 s
  timeout, max 1 MiB line.
- Socket path resolution: `HERDR_SOCKET_PATH` env →
  `~/.config/herdr/sessions/$HERDR_SESSION/herdr.sock` →
  `~/.config/herdr/herdr.sock`.
- `pane.agent_status_changed` subscriptions are **per-pane** (`pane_id`
  required); there is no global variant. Global subscriptions used here:
  `pane.created`, `pane.closed`, `pane.exited`, `pane.agent_detected`.
- The subscription set of a connection is **fixed at subscribe time**; adding
  a pane means opening a replacement stream connection.
- At subscribe time herdr probes each `agent_status_changed` pane
  (`pane.get`) and replays events queued during the setup window — no gap
  between a bootstrap snapshot and the stream, per pane.
- Events are delivered by a server-side 100 ms poll loop; the event hub
  retains the last **512** events (a slow/stalled consumer can miss events —
  hence the resync timer).
- `pane.agent_status_changed` events fire for **presentation changes too**
  (metadata set/expiry), not just lifecycle changes, and carry
  `agent_status`, `agent`, `custom_status`, `title`, `display_agent`,
  `state_labels`.
- `session.snapshot` returns version/protocol metadata plus workspace, tab,
  pane, layout, and **agent records** in one response. Servers < 0.7.2 reply
  with an unknown-method error.
- `pane.report_metadata` is display-only: it never affects waits,
  notifications, rollups, or `agent_status`. `custom_status` is normalized
  and capped at 32 chars; `ttl_ms` ∈ [1, 86400000]; on TTL expiry herdr
  removes that source's metadata itself.
- The sidebar renders `custom_status` for **every** agent state, including
  `done` (`src/ui/sidebar.rs`, unconditional append after the state label).

## Decisions

1. Full migration: no `herdr` CLI subprocess anywhere in the daemon.
2. Included in scope: done-pane ⏳ labels **and** moving progress labels to
   `report_metadata` (both were approved; progress migration deletes the
   `agent_explain` masking workaround).
3. Require herdr ≥ 0.7.2; no dual-mode transport.
4. Single-threaded sync loop over `selectors` — no asyncio, no threads, no
   new dependencies.
5. **Events are triggers, snapshots are truth.** Only `agent_status_changed`
   is handled incrementally (hot path). All lifecycle events (`pane.created`,
   `pane.closed`, `pane.exited`, `pane.agent_detected`) merely schedule an
   immediate resync + resubscribe. This avoids incremental-registry bugs.

## Architecture

### `herdr_socket.py` (new)

Low-level transport, stdlib only:

- `resolve_socket_path() -> str` — resolution order above.
- `request(method: str, params: dict, *, timeout_s: float = 10.0) -> dict` —
  one connection per call; returns the `result` object. Raises
  `HerdrApiError(code, message)` for error responses and
  `HerdrUnavailable` for connect/timeout/EOF failures.
- `class EventStream` — opens a connection, sends one `events.subscribe`
  with the given subscription list, reads the ack (raises on error ack).
  Exposes `fileno()` for `selectors`, `read_events() -> list[dict]`
  (non-blocking drain of complete lines; partial line buffered), and
  `close()`. EOF surfaces as a `StreamClosed` sentinel from `read_events()`.

### `herdr.py` (`HerdrClient`, rewritten internals, same role)

Same injectable facade as today, but built on `request()`:

- `session_snapshot() -> dict`
- `agent_get(pane_id) -> dict` (fresh record at probe time: cwd,
  foreground_cwd, agent, agent_session, agent_status)
- `report_agent(pane_id, source, agent, state, custom_status)` — unchanged
- `release_agent(pane_id, source, agent)` — unchanged
- `report_metadata(pane_id, source, *, agent=None, custom_status=None,
  clear_custom_status=False, ttl_ms=None) -> bool` — new
- `pane_process_info(pane_id) -> dict` (bgjobs probe)
- **Deleted:** `agent_explain()` — only consumer was the progress path's
  masking workaround, which the metadata migration removes.

Booleans-on-failure semantics stay as today (a failed write returns False and
the daemon retries at the next opportunity) so the daemon's retry logic ports
unchanged.

### `daemon.py` (loop rewritten, per-pane logic kept)

State:

- `registry: dict[pane_id, AgentRecord]` — last known agent records, built
  from `session.snapshot`, status updated by `agent_status_changed` events.
- `managed: dict[pane_id, ManagedPane]` — as today, `kind` gains a value:
  - `"hold"` — idle pane held `working` via `report_agent` (unchanged).
  - `"progress"` — working pane's task label, now via `report_metadata`.
  - `"done"` — done pane's ⏳ label via `report_metadata`.
- `_session_cache` — unchanged (herdr still omits `agent_session` for
  working panes).
- Timers per pane (`_last_probe`) and two global deadlines: next reprobe
  sweep and next resync.

Main loop (single thread):

1. **Bootstrap:** `session.snapshot` → registry; adopt `"hold"` panes from
   the state file (same pid-liveness rule as today); open `EventStream`
   (4 global lifecycle subs + one `agent_status_changed` sub per registry
   pane); run one full sweep.
2. **Wait:** `selectors` on the stream fd, timeout = time to the nearest
   deadline (per-pane reprobe, resync).
3. **`agent_status_changed` event:** update registry; run the same per-pane
   decision the old `tick()` made for that pane (progress path for working,
   hold path for idle, done path for done). A transition **into** `idle`
   clears the pane's probe throttle so the probe runs immediately.
4. **Lifecycle event:** schedule resync now.
5. **Resync (timer ~`resync_interval_s`, default 60 s, and after every
   reconnect/lifecycle event):** `session.snapshot`; reconcile vanished
   managed panes (below); replace registry; if the pane set changed, open a
   replacement `EventStream` and close the old one; drop stale
   `_last_probe`/`_session_cache` entries.

   **Vanished panes:** when a *successful* snapshot lacks a managed pane,
   herdr itself has dropped the pane (and any assertion on it) — make one
   best-effort release/clear and **drop the bookkeeping regardless of the
   outcome**; retrying against a nonexistent pane would leave a stale
   managed entry forever. Retry-on-failed-release applies only to panes the
   snapshot still contains. A release/clear that returns a structured
   `not_found` error is likewise treated as cleared. If the snapshot
   request itself failed (herdr down), keep all state and retry later — a
   blip must not orphan or drop assertions.
6. **Reprobe sweep:** at the unchanged 15 s cadence (per-pane throttle),
   probe **(a) every managed `"hold"` and `"done"` pane and (b) every
   unmanaged eligible `idle`/`done` registry pane**. Both halves matter:
   a held pane reports `working` (our own authoritative assertion), so an
   idle/done status filter alone would never reprobe it and the hold would
   never release; and pending work can begin with no herdr event — a marker
   added via `herdwatch add` to an already-idle pane, CI triggered late —
   so sweeping only managed panes would never discover it (today's poll
   loop covers both; this preserves that). `"progress"` panes are owned by
   the progress sweep. The reprobe sweep also refreshes `"done"` metadata
   TTLs (see below).
7. **Progress sweep (new timer):** progress labels change when the agent's
   local session file changes, which produces **no herdr event** — so
   event-driven alone would never update them. A dedicated sweep (default
   4 s, `[progress] interval_s`) iterates registry panes with
   `status == working` and `agent == claude`, re-derives the label (local
   file read, no herdr request), and writes metadata only when the label
   changed — plus a TTL refresh write when half the TTL has elapsed.

**Self-echo guard:** our own `report_agent`/`report_metadata` writes come
back as `agent_status_changed` presentation events. Events whose
(`agent_status`, `custom_status`) equal the state we just asserted for that
pane are treated as acks: registry is updated, no probe is scheduled. The
per-pane probe throttle is the backstop against any remaining loop.

### Per-pane behavior

| Pane state | Pending work | Action (source `herdwatch`) |
| --- | --- | --- |
| `idle` | yes | `report_agent working` + ⏳ label — **unchanged** |
| `idle` | no | `release_agent` if held — unchanged |
| `done` | yes | `report_metadata custom_status="⏳ …" ttl_ms=2×reprobe` (refreshed each reprobe); semantic state stays `done` |
| `done` | no / cleared | `report_metadata clear_custom_status` |
| `done` → `idle` (viewed) | yes | clear metadata, normal hold takes over |
| `working` (claude, active task list) | — | `report_metadata custom_status=<progress>` + same TTL pattern (was `report_agent working`) |
| `working`, task list gone | — | `report_metadata clear_custom_status` |

TTL = `2 × reprobe_interval_s × 1000` ms, clamped to herdr's accepted
`ttl_ms` range — effectively `[1000, 86_400_000]` — so a misconfigured
`reprobe_interval_s` (0, negative, > 12 h) cannot produce an invalid
request. Because metadata self-expires,
`"progress"` and `"done"` kinds are **not re-adopted** after a crash — TTL
cleans up orphans, and the next event/resync re-asserts if still warranted.
Only `"hold"` (a semantic assertion with no TTL) keeps the adopt path. All
kinds stay in the state file so `herdwatch status` can show them.

`herdwatch status` verb per kind: `holding` (hold), `working` (progress),
`labeling` (done).

## Error handling & recovery

- **Subscribe rejected** (error ack — e.g. a pane closed between snapshot
  and subscribe, so herdr's setup probe fails) → treat like a lifecycle
  event: resync immediately and subscribe again with the fresh pane set.
- **Stream EOF/error** → close, reconnect with exponential backoff
  (0.5 s base, 30 s cap; herdeck `connector.py` pattern), then full
  re-bootstrap (snapshot + subscribe + sweep). Log once per distinct failure
  reason, not per retry.
- **`request()` failure** (herdr down/restarting) → same semantics as
  today's `rc != 0`: asserts aren't recorded, releases keep bookkeeping and
  retry; the failed pane's throttle is cleared so retry is prompt.
- **Old server** (unknown `session.snapshot`) → log one clear message
  ("herdwatch requires herdr ≥ 0.7.2, server reports …"), retry with the
  same backoff. The daemon never exits over this (launchd KeepAlive stays
  meaningful).
- **Missed events** (hub overflow, subscribe gaps) → covered by the resync
  timer and the reprobe sweep; correctness never depends on seeing every
  event.
- **Clean shutdown** (SIGTERM/atexit): `release_agent` all `"hold"` panes
  and `clear_custom_status` all `"progress"`/`"done"` panes.

## Config & compatibility

- `resync_interval_s` (new, default 60.0) under `[daemon]`; `interval_s`
  (new, default 4.0) under `[progress]`.
- `poll_interval_s` — no longer used; if present in config, log a one-line
  deprecation notice and ignore it. `reprobe_interval_s` unchanged.
- `herdr-plugin.toml`: `min_herdr_version = "0.7.2"`.
- `doctor` gains: socket path exists + connectable, `ping` round-trip,
  server protocol ≥ 16 (via `session.snapshot` version metadata).
- README: rewrite "How it works" (event-driven, raw socket), drop v1
  limitations #1 and #2, soften #5 (progress labels no longer mask state;
  the ⏳ hold still does), note the herdr ≥ 0.7.2 requirement.

## Testing

- `test_herdr_socket.py`: real unix socket served by a stdlib fixture —
  request/response, error-response mapping, `HerdrUnavailable` on dead
  socket, subscribe ack + pushed events, partial-line buffering, EOF.
- `test_daemon.py`: port existing scenarios; entry point changes from
  `tick()` to feeding events/advancing the fake clock through the dispatch,
  with a fake client + fake stream. Every current behavior
  (throttle, adopt, failed release retry, vanished panes, progress,
  allow/deny) must keep a test. New tests: done-pane metadata lifecycle,
  TTL refresh + clamp boundaries, self-echo guard, progress sweep (label
  change with no herdr event), unmanaged idle/done pane picked up by the
  reprobe sweep (late marker), held pane (status `working` from our own
  assertion) still reprobed and released when work clears, vanished-pane
  bookkeeping drop vs herdr-down retention, resubscribe on pane-set change,
  reconnect re-bootstrap, old-server retry loop.
- Live verification against the running herdr 0.7.3 before merge (verify
  skill): idle-edge latency, done-pane ⏳ visible, progress label without
  state masking, daemon restart reconciliation.

## Future (out of scope)

- `state_labels` override for done panes (e.g. `done` → "waiting CI").
- Live `herdwatch status` over a control socket (config `socket_path` stays
  reserved).
- Windows named-pipe transport.
