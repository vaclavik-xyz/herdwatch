# src/herdwatch/doctor.py
"""Environment diagnostics for herdwatch (`herdwatch doctor`)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from .service import PLIST_PATH  # single source of truth for the launchd plist path


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str = ""


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def _list_procs() -> list[str]:
    try:
        r = subprocess.run(["ps", "-eo", "command="], capture_output=True, text=True, timeout=5)
        return r.stdout.splitlines() if r.returncode == 0 else []
    except Exception:
        return []


def run_checks(*, which: Callable[[str], bool], run: Callable[[list[str]], tuple[int, str]],
               list_procs: Callable[[], list[str]], plist_path: str) -> list[Check]:
    checks: list[Check] = []

    herdr = which("herdr")
    checks.append(Check("herdr on PATH", herdr, True,
                        "" if herdr else "install herdr — https://herdr.dev"))

    status_rc, status_out = run(["herdr", "status"]) if herdr else (1, "")
    running = bool(herdr) and status_rc == 0 and "running" in status_out.lower()
    checks.append(Check("herdr server running", running, True,
                        "" if running else "start herdr (run `herdr`)"))

    gh = which("gh") and run(["gh", "auth", "status"])[0] == 0
    checks.append(Check("gh authenticated (CI probe)", bool(gh), False,
                        "" if gh else "optional: run `gh auth login` to enable the CI probe"))

    roborev = which("roborev")
    checks.append(Check("roborev present (review probe)", roborev, False,
                        "" if roborev else "optional: install roborev to enable the review probe"))

    daemon = any("herdwatch" in p and "daemon" in p for p in list_procs())
    checks.append(Check("herdwatch daemon running", daemon, False,
                        "" if daemon else "run `herdwatch install-service` (macOS) or `herdwatch daemon`"))

    svc = os.path.exists(plist_path)
    checks.append(Check("launchd service installed", svc, False,
                        plist_path if svc else "run `herdwatch install-service`"))

    return checks


def diagnose() -> list[Check]:
    return run_checks(which=_which, run=_run, list_procs=_list_procs, plist_path=PLIST_PATH)


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.required else "⚠")  # ✓ ✗ ⚠
        line = f"{mark} {c.name}"
        if c.detail:
            line += f"  — {c.detail}"
        lines.append(line)
    return "\n".join(lines)


def to_json(checks: list[Check]) -> str:
    return json.dumps([{"name": c.name, "ok": c.ok, "required": c.required, "detail": c.detail}
                       for c in checks])


def exit_code(checks: list[Check]) -> int:
    return 0 if all(c.ok for c in checks if c.required) else 1
