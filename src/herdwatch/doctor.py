# src/herdwatch/doctor.py
"""Environment diagnostics for herdwatch (`herdwatch doctor`)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from typing import Callable

from . import herdr_socket
from .herdr_socket import HerdrApiError, HerdrUnavailable
from .service import PLIST_PATH  # single source of truth for the launchd plist path

HERDR_CONFIG_PATH = os.path.expanduser("~/.config/herdr/config.toml")
SIDEBAR_CHECK = "herdr sidebar shows $waiting_on"
SIDEBAR_HINT = (
    "herdwatch labels are invisible in herdr's default sidebar; add "
    '`["$waiting_on", "$progress"]` to [ui.sidebar.agents] rows '
    "(see README) and run `herdr server reload-config`"
)
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


def _row_tokens(rows) -> set[str]:
    tokens = set()
    if not isinstance(rows, list):
        return tokens
    for row in rows:
        for entry in row if isinstance(row, list) else ():
            if isinstance(entry, dict):
                entry = entry.get("token")
            if isinstance(entry, str):
                tokens.add(entry)
    return tokens


def sidebar_shows_waiting(config_text: str | None) -> bool:
    """Whether herdr's Agent sidebar layout renders the waiting_on token.

    Herdr's default rows show only built-ins, so metadata tokens stay
    invisible until the user adds them to `ui.sidebar.agents.rows` (or to
    the Claude override in `rows_by_agent`).
    """
    if not config_text:
        return False
    try:
        data = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return False
    agents = data
    for key in ("ui", "sidebar", "agents"):
        agents = agents.get(key) if isinstance(agents, dict) else None
    if not isinstance(agents, dict):
        return False
    layouts = [agents.get("rows")]
    by_agent = agents.get("rows_by_agent")
    if isinstance(by_agent, dict):
        layouts.extend(by_agent.values())
    return any("$waiting_on" in _row_tokens(rows) for rows in layouts)


def _read_herdr_config() -> str | None:
    try:
        with open(HERDR_CONFIG_PATH, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _snapshot() -> dict:
    return herdr_socket.request("session.snapshot", {})


def run_checks(*, which: Callable[[str], bool], run: Callable[[list[str]], tuple[int, str]],
               list_procs: Callable[[], list[str]], plist_path: str,
               snapshot: Callable[[], dict],
               herdr_config: Callable[[], str | None] = lambda: None) -> list[Check]:
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

    sidebar = sidebar_shows_waiting(herdr_config())
    checks.append(Check(SIDEBAR_CHECK, sidebar, False,
                        "" if sidebar else SIDEBAR_HINT))

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
                      snapshot=_snapshot, herdr_config=_read_herdr_config)


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
