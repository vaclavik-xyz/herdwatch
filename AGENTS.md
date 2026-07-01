# AGENTS.md — herdwatch

herdwatch is a background daemon that keeps [herdr](https://herdr.dev) agent
panes shown as `working ⏳` while background work (CI, code review, jobs) is
still pending after the agent goes idle — so a finished-looking pane isn't
mistaken for a done one. This file tells a coding agent how to set it up on the
**current machine**.

## Setup (run these in order)

1. **Install the package** from this checkout:

   ```
   python3 -m venv .venv && .venv/bin/pip install .
   ```

   This creates a `herdwatch` command at `.venv/bin/herdwatch`. (For a
   user-wide command instead, use `pipx install .`.)

2. **Check the environment:**

   ```
   .venv/bin/herdwatch doctor
   ```

   The two **required** checks — `herdr on PATH` and `herdr server running` —
   must be ✓. If they aren't, herdr itself isn't installed/running: stop and
   tell the user, don't try to install herdr yourself. `gh` and `roborev` are
   **optional** (they enable the CI and review probes); a ⚠ there is fine.
   `doctor` exits 0 when required checks pass, 1 otherwise, and supports
   `--json` for machine parsing.

3. **Install the background service** (macOS):

   ```
   .venv/bin/herdwatch install-service
   ```

   This generates a launchd plist with the correct paths for **this** machine
   and loads it, so the daemon runs persistently and restarts at login. Preview
   it first with `install-service --dry-run`; tear it down later with
   `install-service --uninstall`.

   **Linux:** there is no launchd. Run `herdwatch daemon` under a supervisor
   (e.g. a systemd user unit) instead.

4. **Verify:**

   ```
   .venv/bin/herdwatch doctor
   ```

   `herdwatch daemon running` and `launchd service installed` should now be ✓.

That's the whole setup — there is **no per-agent configuration**. herdwatch
watches every herdr pane automatically and works for any agent herdr tracks.

## Alternative: install as a herdr plugin

```
herdr plugin install vaclavik-xyz/herdwatch
herdr plugin pane open --plugin herdwatch --entrypoint daemon
```

## Prerequisites

- **herdr** running — required (https://herdr.dev)
- **gh**, authenticated — optional, enables the CI probe (`gh auth login`)
- **roborev** — optional, enables the code-review probe

A missing optional tool just disables its probe; it never blocks a pane.

## Config (optional)

`~/.config/herdwatch/config.toml` — enable/disable probes, intervals, and
per-pane `allow`/`deny`. All probes are on by default, so this file is usually
not needed.

## Conventions (for agents editing this repository)

- Python ≥3.11, **stdlib-only runtime** (test dependency: `pytest`).
- Run the tests: `.venv/bin/python -m pytest`.
- Every external-tool call (`herdr`, `gh`, `roborev`, `ps`, `launchctl`,
  filesystem) is **dependency-injected** so it is unit-testable with fakes —
  keep that pattern when adding code.
- Conventional commits, English, no `Co-Authored-By`.
- Design spec and implementation plan live in `docs/superpowers/`.
