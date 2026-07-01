# herdwatch

Keeps a [herdr](https://herdr.dev) pane shown as **working** with a `⏳` status
while background work (CI, roborev review, background jobs, manual markers) is
still pending after the agent went idle — so a finished-looking pane isn't
mistaken for a done one.

> **Setting this up via a coding agent?** Point it at [AGENTS.md](AGENTS.md) — a
> runbook it can follow to install, enable, and verify herdwatch on your machine.

## How it works

herdwatch is a standalone background daemon — **not** a herdr fork and not a
screen-scraper. It talks to herdr over its socket/CLI (herdr's documented plugin
API surface), polls `herdr agent list`, and for panes that went idle/done runs a
set of probes. While any probe is pending it asserts `working` + a `⏳` label via
`herdr pane report-agent`; when they clear it releases the pane. No changes to
herdr, no per-agent setup, and it works for any agent herdr tracks.

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
    herdwatch status         # list active markers
    herdwatch rm <id>

## Config

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals, per-pane
`allow`/`deny`. See `docs/superpowers/specs/2026-07-01-herdwatch-design.md`.

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
