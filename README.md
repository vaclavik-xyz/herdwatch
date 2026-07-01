# herdwatch

Keeps a [herdr](https://herdr.dev) pane shown as **working** with a `⏳` status
while background work (CI, roborev review, background jobs, manual markers) is
still pending after the agent went idle — so a finished-looking pane isn't
mistaken for a done one.

## Install

    pip install -e ".[dev]"

## Run

    herdwatch daemon          # supervise via deploy/dev.herdwatch.daemon.plist

## Manual markers

    herdwatch add "deploy" --until 'gh run watch --exit-status'
    herdwatch add "backup" --ttl 600
    herdwatch list
    herdwatch status         # list active markers
    herdwatch rm <id>

## Config

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals. See
`docs/superpowers/specs/2026-07-01-herdwatch-design.md`.

## Install the launchd agent

    cp deploy/dev.herdwatch.daemon.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/dev.herdwatch.daemon.plist

## v1 limitations

- **Poll-based, not event-driven.** The daemon polls `herdr agent list` every
  `poll_interval_s` (~4s). A pane can briefly show its own "done" before
  herdwatch re-marks it working (a sub-poll-interval window).
- **`status` shows markers only.** The daemon runs as a separate process, so
  `herdwatch status` cannot show its in-memory managed-pane set — only active
  markers. (`socket_path` in config is reserved for a future status channel and
  is currently unused.)
- **No cross-restart reconciliation.** On clean shutdown (SIGTERM / launchctl
  unload) herdwatch releases all panes it manages. But if the daemon is killed
  uncleanly *and* the background work finishes while it's down, a pane can be
  left showing `working ⏳` until it next becomes busy-then-idle. A future
  version will reconcile herdwatch-owned assertions on startup.
- **No "step aside" on resumed work.** While herdwatch asserts `working`, its
  own assertion masks the agent's real status, so it cannot detect the human
  resuming genuine work mid-wait; the ⏳ label persists until the background
  work clears.
