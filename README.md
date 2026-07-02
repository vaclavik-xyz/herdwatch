# herdwatch

[![CI](https://github.com/vaclavik-xyz/herdwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/vaclavik-xyz/herdwatch/actions/workflows/ci.yml)

## The problem

Your coding agent finishes its turn, so [herdr](https://herdr.dev) shows the
pane `idle`/`done`. But the work isn't actually done: you just merged and CI is
running, a post-commit review is in flight, or a job is still going in the
background. You glance at the sidebar, see "done", switch to that pane — and
nothing's happening, because the real work is off-screen. So a pane that *looks*
finished isn't, and you can't trust the sidebar at a glance.

herdwatch fixes that: while background work is still pending after an agent goes
idle, it keeps that pane shown as **working** with a `⏳` label saying what it's
waiting on (CI, roborev review, a manual marker, or — opt-in — a background job),
and releases it the moment the work clears.

> **Setting this up via a coding agent?** Point it at [AGENTS.md](AGENTS.md) — a
> runbook it can follow to install, enable, and verify herdwatch on your machine.

## How it works

herdwatch is a standalone background daemon — **not** a herdr fork and not a
screen-scraper. It talks to herdr over its socket/CLI (herdr's documented plugin
API surface), polls `herdr agent list`, and for panes that went idle/done runs a
set of probes. While any probe is pending it asserts `working` + a `⏳` label via
`herdr pane report-agent`; when they clear it releases the pane. No changes to
herdr, no per-agent setup, and it works for any agent herdr tracks.

**The key trick** (reusable for any similar tool): a state reported through
`herdr pane report-agent --source <name>` is *authoritative and durable over
herdr's own screen-detection* for as long as that source holds it. So herdwatch
never fights the screen scraper — it just asserts `working` from its own source
and later releases, and herdr honours it. That single property is what makes a
non-invasive "hold this pane" daemon possible without forking herdr.

The daemon also publishes the set of panes it is currently holding (and the
recorded `⏳` label per pane) to a small JSON state file
(`~/.local/state/herdwatch/managed.json`), so `herdwatch status` — a separate
process — can show what herdwatch is holding right now. The snapshot records the
daemon's pid, so `status` can tell a live snapshot from one a dead daemon left
behind. On startup the daemon reads that file back and re-adopts those panes, so
a crash-and-restart reconciles them (next tick re-probes → re-asserts or
releases) instead of orphaning a `working ⏳`.

## Task progress in the sidebar

While a Claude Code agent is actively working through a task list, herdwatch
shows how far along it is — `3/7 Fixing auth bug` — as the pane's status
label. It reads the session's task files (`~/.claude/tasks/`, matched via
herdr's `agent_session` id), so no per-agent setup is needed; other agents
are skipped. Because herdwatch asserts the label over herdr's own detection,
it re-checks the pane each tick with `herdr agent explain` (live screen
detection) and drops the label the moment the agent actually stops — a pane
waiting for your input is never masked. Disable with:

```toml
[progress]
enabled = false
```

## Install & run

**From source (recommended for now):**

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
poll_interval_s = 4         # how often to re-check herdr
reprobe_interval_s = 15     # min seconds between probing the same pane

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
enabled = true               # default

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

- **Poll-based, not event-driven.** The daemon polls `herdr agent list` every
  `poll_interval_s` (~4s). A pane can briefly show its own "done" before
  herdwatch re-marks it working (a sub-poll-interval window).
- **`status` is a snapshot, not a live query.** `herdwatch status` reads the
  state file the daemon writes each tick, so it lags reality by up to one
  `poll_interval_s`. If the daemon died uncleanly the file lingers, but `status`
  flags this by checking the recorded pid. (`socket_path` in config is reserved
  for a future live status channel and is currently unused.)
- **Recovery depends on the state file.** On clean shutdown (SIGTERM / launchctl
  unload) herdwatch releases every pane it manages. After an *unclean* death it
  reconciles on restart by re-adopting the panes from
  `~/.local/state/herdwatch/managed.json` — but if that file is deleted while the
  daemon is down, any pane held at crash time is left showing `working ⏳` until
  it next becomes busy-then-idle.
- **No "step aside" on resumed work.** While herdwatch asserts `working`, its
  own assertion masks the agent's real status, so it cannot detect the human
  resuming genuine work mid-wait; the ⏳ label persists until the background
  work clears.
