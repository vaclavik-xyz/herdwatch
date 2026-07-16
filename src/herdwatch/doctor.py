# src/herdwatch/doctor.py
"""Environment diagnostics for herdwatch (`herdwatch doctor`)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from . import herdr_socket
from .herdr_socket import HerdrApiError, HerdrUnavailable
from .service import PLIST_PATH  # single source of truth for the launchd plist path

MIN_HERDR_VERSION = (0, 7, 4)
HERDR_VERSION_CHECK = "herdr >= 0.7.4 (metadata tokens)"


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


def _snapshot() -> dict:
    return herdr_socket.request("session.snapshot", {})


def run_checks(*, which: Callable[[str], bool], run: Callable[[list[str]], tuple[int, str]],
               list_procs: Callable[[], list[str]], plist_path: str,
               snapshot: Callable[[], dict]) -> list[Check]:
    checks: list[Check] = []

    herdr = which("herdr")
    checks.append(Check("herdr on PATH", herdr, True,
                        "" if herdr else "install herdr — https://herdr.dev"))

    status_rc, status_out = run(["herdr", "status"]) if herdr else (1, "")
    running = bool(herdr) and status_rc == 0 and "running" in status_out.lower()
    checks.append(Check("herdr server running", running, True,
                        "" if running else "start herdr (run `herdr`)"))

    version_rc, version_out = run(["herdr", "--version"]) if herdr else (1, "")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_out)
    version = tuple(map(int, match.groups())) if version_rc == 0 and match else None
    modern = version is not None and version >= MIN_HERDR_VERSION
    detail_ver = (
        ""
        if modern
        else f"herdwatch requires herdr >= 0.7.4; got {version_out.strip() or 'unknown'}"
    )
    reachable = False
    snapshot_ok = False
    detail_sock = ""
    try:
        result = snapshot()
        reachable = True
        if isinstance(result, dict) and isinstance(result.get("snapshot"), dict):
            snapshot_ok = True
        else:
            detail_ver = (
                "session.snapshot returned an unusable payload "
                "(missing snapshot object)"
            )
    except HerdrApiError as exc:
        reachable = True
        if exc.code == "unknown_method":
            detail_ver = (
                f"server rejected session.snapshot ({exc.code}); "
                "herdwatch requires herdr >= 0.7.4 — run `herdr update`"
            )
        else:
            detail_ver = f"session.snapshot failed ({exc.code}): {exc.message}"
    except HerdrUnavailable as exc:
        detail_sock = f"cannot reach {herdr_socket.resolve_socket_path()}: {exc}"
        detail_ver = "unreachable"
    checks.append(Check("herdr socket reachable", reachable, True, detail_sock))
    checks.append(
        Check(HERDR_VERSION_CHECK, modern and snapshot_ok, True, detail_ver)
    )

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
    return run_checks(which=_which, run=_run, list_procs=_list_procs, plist_path=PLIST_PATH,
                      snapshot=_snapshot)


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
