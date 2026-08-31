<p align="center">
  <img src="assets/brand/herdwatch-logo.svg" alt="herdwatch" width="560">
</p>

<p align="center">
  <a href="https://github.com/vaclavik-xyz/herdwatch/actions/workflows/ci.yml"><img src="https://github.com/vaclavik-xyz/herdwatch/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

## The problem

Your coding agent finishes its turn, so [herdr](https://herdr.dev) shows the
pane `idle`/`done`. But the work isn't actually done: you just merged and CI is
running, a post-commit review is in flight, or a job is still going in the
background. You glance at the sidebar, see "done", switch to that pane — and
nothing's happening, because the real work is off-screen. So a pane that *looks*
finished isn't, and you can't trust the sidebar at a glance.

herdwatch fixes that: while background work is still pending after an agent goes
idle, it adds a `⏳` label saying what it is waiting on (CI, roborev review, a
manual marker, or — opt-in — a background job). The label is TTL-backed display
metadata: herdr and its official integrations remain the sole owners of the
pane's real lifecycle state, while Herdeck derives `WAITING` from `waiting_on`.

> **Setting this up via a coding agent?** Point it at [AGENTS.md](AGENTS.md) — a
> runbook it can follow to install, enable, and verify herdwatch on your machine.

## How it works

herdwatch is a standalone background daemon — **not** a herdr fork and not a
screen-scraper. It requires **herdr ≥ 0.7.4** and talks directly to herdr's
socket API. It bootstraps from `session.snapshot`, subscribes to herdr's socket
events (`pane.agent_status_changed` for every pane plus lifecycle events),
reacts to idle/done edges promptly, and re-verifies against a fresh snapshot
every `resync_interval_s` (60 s by default), so correctness never depends on
seeing every event. For idle and done panes, herdwatch runs a set of probes; while
any probe is pending it publishes the description as the `waiting_on` metadata
token. No per-agent setup is needed.

CI runs are assigned to one pane instead of broadcast to every pane that shares
the repository. An exact checkout HEAD match wins; when agents operate on a
linked worktree while their pane stays in the main checkout, herdwatch falls
back to the repository's only actively working pane and keeps that assignment
when the agent becomes idle. Ambiguous runs are left unassigned rather than
labeling unrelated panes. A working owner receives the CI description as the
display-only `waiting_on` token, so its real lifecycle state remains `working`.

Herdwatch deliberately replaces the replay-heavy global
`pane.agent_detected` feed with status subscriptions on all panes, including
currently unknown ones. The first `unknown → working` edge still triggers an
immediate snapshot, without destabilizing status delivery for known agents.

A pane herdr reports as `idle` or `done` keeps that semantic state. If work is
still pending, herdwatch adds a **display-only** `waiting_on` token via
`pane.report_metadata`. The token has a bounded TTL and is refreshed only while
the probe still reports pending work, so a stopped daemon cannot leave a stale
label permanently.

An optional compatibility mode can still request a semantic `working` hold for
unmanaged panes. It is disabled by default because an official integration may
own the pane session and must not be impersonated or released. When explicitly
enabled, herdwatch verifies that Herdr applied its request and falls back to
metadata if another source owns the session.

The daemon also publishes the set of panes it is currently managing (and the
recorded `⏳` label per pane) to a small JSON state file
(`~/.local/state/herdwatch/managed.json`), so `herdwatch status` — a separate
process — can show what herdwatch is labeling right now. The snapshot records the
daemon's pid, so `status` can tell a live snapshot from one a dead daemon left
behind. Metadata rows need no crash recovery because their TTL self-cleans.
When upgrading from a version that persisted a semantic hold, the daemon
re-adopts and releases that legacy assertion before switching to metadata.

## Optional task progress in the sidebar

When enabled, while a Claude Code agent is actively working through a task list, herdwatch
shows how far along it is — `3/7 Fixing auth bug` — through the pane's
`progress` token. It reads the session's task files (`~/.claude/tasks/`, matched via
herdr's `agent_session` id), so no per-agent setup is needed; other agents
are skipped. Progress is display-only metadata published through
`pane.report_metadata`; herdr keeps detecting the real lifecycle state beneath
it, so the label never masks `blocked` or `idle`. Enable with:

```toml
[progress]
enabled = true
```

## Install & run

**With pipx:**

    pipx install herdwatch
    herdwatch doctor
    herdwatch daemon

**From source:**

    git clone https://github.com/vaclavik-xyz/herdwatch && cd herdwatch
    python3 -m venv .venv && .venv/bin/pip install .
    .venv/bin/herdwatch doctor            # check herdr is reachable + what's set up
    .venv/bin/herdwatch daemon            # run in the foreground to try it

Prerequisites: a running herdr; optionally `gh` (authenticated) for the CI probe
and `roborev` for the review probe. A missing tool just disables its probe — it
never blocks a pane.

**As a launchd service (auto-start / auto-restart), macOS:**

    herdwatch install-service              # generate a plist with the right paths for THIS machine + load it
    herdwatch install-service --dry-run    # preview the plist first
    herdwatch install-service --uninstall  # unload + remove

(`deploy/dev.herdwatch.daemon.plist` is only a static example; `install-service`
generates the real one so the paths are correct on any machine. Unloading the
service releases all panes herdwatch manages.)

**As a herdr plugin** (`herdr-plugin.toml` is included):

    herdr plugin install vaclavik-xyz/herdwatch   # clones + builds a local venv
    herdr plugin pane open --plugin herdwatch --entrypoint daemon

The plugin build creates a `.venv` and installs the package; the `daemon` pane
runs the watcher inside herdr (no launchd needed). `status` and `list-markers`
actions are registered too.

## Manual markers

    herdwatch add "deploy" --until 'gh run watch --exit-status'
    herdwatch add "backup" --ttl 600
    herdwatch list
    herdwatch status         # what the daemon holds right now + active markers
    herdwatch rm <id>

## Config

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals, per-pane
`allow`/`deny`, and per-probe tuning. Everything has a sensible default; the
file is optional. The full set of keys:

```toml
[daemon]
resync_interval_s = 60      # fresh snapshot safety-net interval
reprobe_interval_s = 15     # min seconds between probing the same pane

[lifecycle]
semantic_holds = false      # default; true is compatibility mode for unmanaged panes

[probes]
ci = true                   # on by default: roborev, ci, marker
roborev = true              # bgjobs is OFF by default (opt-in below)

# Per-probe tuning goes in its own table. Because TOML forbids a key that is
# both a value and a table, enable/disable a tuned probe with `enabled` INSIDE
# its table (not `bgjobs = true` under [probes] as well).
[probes.bgjobs]
enabled = true              # opt in to background-job detection
min_age_s = 5               # ignore just-spawned processes
ignore = ["vite", "webpack"]  # extra process names to treat as "not a job"
                              # (added on top of the built-in defaults)

[progress]
enabled = false              # opt in to Claude Code task-file progress
interval_s = 4               # how often to refresh task progress

[panes]
allow = []                  # if non-empty, only manage these pane ids
deny  = []                  # never manage these pane ids
```

**Why bgjobs is opt-in:** herdr is an agent multiplexer, so every pane runs an
agent, and agents constantly spawn short-lived subprocesses (`sleep`, `git`,
test runners, an editor daemon, their own runtime). The background-job probe
scans a pane's process tree, so on agent panes it readily mistakes those for
"work" and holds the pane. The reliable signals — CI, roborev, and manual
markers — are on by default; enable bgjobs only on panes where you actually run
long jobs by hand, and use `[probes.bgjobs] ignore` to teach it which process
names to skip.

## v1 limitations

- **Requires herdr ≥ 0.7.4.** The daemon needs socket `session.snapshot`, event
  subscriptions, and named metadata tokens. There is no fallback for older
  metadata fields; `herdwatch doctor` checks this requirement.
- **Herdeck provides the semantic `WAITING` view.** Herdr itself keeps the
  authoritative `idle`/`done` state and the `waiting_on` token beside it.
  Herdeck renders that combination as `WAITING`. Clients that ignore named
  metadata will see the original Herdr lifecycle state and no waiting overlay.
- **`status` is a snapshot, not a live query.** `herdwatch status` reads the
  state file the daemon writes each sweep, so it lags reality by up to one
  sweep interval. If the daemon died uncleanly the file lingers, but `status`
  flags this by checking the recorded pid. (`socket_path` in config is reserved
  for a future live status channel and is currently unused.)
- **Semantic holds are a compatibility option.** With
  `[lifecycle] semantic_holds = true`, unmanaged panes regain the older
  `working ⏳` behavior. Those durable assertions depend on the state file for
  crash recovery and can mask the underlying lifecycle while active. Keep the
  default metadata-only mode unless a client cannot interpret `waiting_on`.

## Herdr API compatibility

CI downloads the SHA-256-verified binaries from Herdr's stable and preview
manifests, exports each binary's JSON API schema, and checks every method,
parameter, subscription, and response field herdwatch consumes. Run the same
check locally with:

    python scripts/check_herdr_api.py --channel stable
    python scripts/check_herdr_api.py --channel preview
