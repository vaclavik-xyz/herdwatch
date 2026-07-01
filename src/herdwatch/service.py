# src/herdwatch/service.py
"""Generate and install a launchd service for the herdwatch daemon (macOS)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

LABEL = "dev.herdwatch.daemon"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>daemon</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{out}</string>
  <key>StandardErrorPath</key><string>{err}</string>
</dict>
</plist>
"""


def is_macos() -> bool:
    return sys.platform == "darwin"


def herdwatch_exe() -> str:
    """Absolute path to the installed `herdwatch` console script for this machine."""
    import shutil
    found = shutil.which("herdwatch")
    if found:
        return found
    return os.path.join(os.path.dirname(sys.executable), "herdwatch")


def default_path_env() -> str:
    """A PATH that reaches herdr (usually ~/.local/bin), homebrew, and herdwatch itself."""
    parts = [
        os.path.dirname(herdwatch_exe()),
        "/opt/homebrew/bin",
        os.path.expanduser("~/.local/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    seen: set[str] = set()
    ordered = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ":".join(ordered)


def render_plist(exe: str | None = None, path_env: str | None = None,
                 out: str = "/tmp/herdwatch.out.log", err: str = "/tmp/herdwatch.err.log") -> str:
    return _PLIST_TEMPLATE.format(
        label=LABEL,
        exe=exe or herdwatch_exe(),
        path=path_env or default_path_env(),
        out=out,
        err=err,
    )


def _write(path: str, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _run(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr)
    except Exception:
        return 1, ""


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def install(*, plist_path: str = PLIST_PATH,
            run: Callable[[list[str]], tuple[int, str]] | None = None,
            render: Callable[[], str] | None = None,
            write: Callable[[str, str], None] | None = None) -> tuple[int, str]:
    # None-sentinels resolved at call time so module-attribute monkeypatching of
    # _run/_write/render_plist actually intercepts (keeps tests off the real system).
    run = run or _run
    render = render or render_plist
    write = write or _write
    write(plist_path, render())
    run(["launchctl", "unload", plist_path])  # tolerate not-yet-loaded
    rc, output = run(["launchctl", "load", plist_path])
    if rc != 0:
        detail = f": {output}" if output else ""
        return 1, f"wrote {plist_path} but `launchctl load` failed (rc={rc}){detail}; check `herdwatch doctor`"
    return 0, f"installed and loaded {plist_path}"


def uninstall(*, plist_path: str = PLIST_PATH,
              run: Callable[[list[str]], tuple[int, str]] | None = None,
              remove: Callable[[str], None] | None = None) -> tuple[int, str]:
    run = run or _run
    remove = remove or _remove
    run(["launchctl", "unload", plist_path])  # ok if it wasn't loaded
    remove(plist_path)
    return 0, f"unloaded and removed {plist_path}"
