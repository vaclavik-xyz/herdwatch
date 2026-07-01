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
    herdwatch rm <id>

## Config

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals. See
`docs/superpowers/specs/2026-07-01-herdwatch-design.md`.

## Install the launchd agent

    cp deploy/dev.herdwatch.daemon.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/dev.herdwatch.daemon.plist
